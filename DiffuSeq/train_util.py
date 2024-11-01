import copy
import functools
import math
import os
from contextlib import nullcontext

import blobfile as bf
import torch as th
import torch.cuda
from opacus.utils import batch_memory_manager
from tqdm import tqdm

from diffuseq.step_sample import LossAwareSampler, UniformSampler
from diffuseq.utils import logger
from diffuseq.utils.fp16_util import zero_grad
from diffuseq.utils.nn import update_ema

# For ImageNet experiments, this was a good default value.
# We found that the lg_loss_scale quickly climbed to
# 20-21 within the first ~1K steps of training.
INITIAL_LOG_LOSS_SCALE = 20.0


class TrainLoop:
    def __init__(
            self,
            *,
            model,
            diffusion,
            data,
            batch_size,
            microbatch,
            lr,
            ema_rate,
            log_interval,
            save_interval,
            resume_checkpoint,
            schedule_sampler=None,
            weight_decay=0.0,
            learning_steps=0,
            checkpoint_path='',
            gradient_clipping=-1.,
            eval_data=None,
            eval_interval=-1,
            opt=None,
            private=False,
            args=None
    ):
        self.args = args
        self.model = model
        self.diffusion = diffusion
        self.data = data
        self.eval_data = eval_data
        self.batch_size = batch_size
        self.microbatch = microbatch if microbatch > 0 else batch_size
        self.lr = lr
        self.ema_rate = (
            [ema_rate]
            if isinstance(ema_rate, float)
            else [float(x) for x in ema_rate.split(",")]
        )
        self.log_interval = log_interval
        self.eval_interval = eval_interval
        self.save_interval = save_interval
        self.resume_checkpoint = resume_checkpoint
        self.schedule_sampler = schedule_sampler or UniformSampler(diffusion)
        self.weight_decay = weight_decay
        self.learning_steps = learning_steps
        self.gradient_clipping = gradient_clipping
        self.private = private

        self.step = 0
        self.resume_step = 0

        self.model_params = list(self.model.parameters())
        self.master_params = self.model_params
        self.lg_loss_scale = INITIAL_LOG_LOSS_SCALE
        self.sync_cuda = th.cuda.is_available()

        self.checkpoint_path = checkpoint_path  # DEBUG **

        self.opt = opt
        if private:
            for x in self.master_params[:2]:
                x.requires_grad = False
        assert (opt is not None)
        self.ema_params = [
            copy.deepcopy(self.master_params) for _ in range(len(self.ema_rate))
        ]

    def _load_and_sync_parameters(self):
        resume_checkpoint = find_resume_checkpoint() or self.resume_checkpoint

        if resume_checkpoint[-3:] == '.pt':
            self.resume_step = parse_resume_step_from_filename(resume_checkpoint)
            logger.log(f"loading model from checkpoint: {resume_checkpoint}...")
            self.model.load_state_dict(actual_model_path(resume_checkpoint))

    def _load_ema_parameters(self, rate):
        ema_params = copy.deepcopy(self.master_params)

        main_checkpoint = find_resume_checkpoint() or self.resume_checkpoint
        ema_checkpoint = find_ema_checkpoint(main_checkpoint, self.resume_step, rate)
        if ema_checkpoint:
            logger.log(f"loading EMA from checkpoint: {ema_checkpoint}...")
            state_dict = actual_model_path(ema_checkpoint)
            ema_params = self._state_dict_to_master_params(state_dict)

        return ema_params

    def run_loop(self):
        zero_grad(self.model_params)
        self.model.train()

        # memory safe for poisson sampling
        if self.private:
            context_manager = batch_memory_manager.BatchMemoryManager(data_loader=self.data,
                                                                      max_physical_batch_size=64,
                                                                      optimizer=self.opt)
        else:
            context_manager = nullcontext()

        with context_manager as train_dataloader:
            if not self.private:
                train_dataloader = self.data
            pbar = tqdm(total=self.learning_steps)
            for epoch in range(math.ceil(self.learning_steps / (len(self.data)))):

                for data_points in train_dataloader:
                    batch, cond = data_points
                    t, weights = self.schedule_sampler.sample(batch.size()[0], device=torch.device('cuda'))

                    # calculate losses
                    compute_losses = functools.partial(
                        self.diffusion.training_losses,
                        self.model,
                        batch,
                        t,
                        self.private,
                        model_kwargs=cond
                    )
                    losses = compute_losses()
                    if isinstance(self.schedule_sampler, LossAwareSampler):
                        self.schedule_sampler.update_with_all_losses(
                            t, losses["loss"].detach()
                        )
                    loss = (losses["loss"] * weights).mean()
                    log_loss_dict(
                        self.diffusion, t, {k: v * weights for k, v in losses.items()}
                    )

                    # update lr
                    if not self.learning_steps:
                        return
                    frac_done = (self.step + self.resume_step) / self.learning_steps
                    lr = self.lr * (1 - frac_done)

                    for param_group in self.opt.param_groups:
                        param_group["lr"] = lr

                    # optimizer step
                    loss.backward()
                    if self.private:
                        self.model._module.position_embeddings.weight.grad_sample = (
                            self.model._module.position_embeddings.weight.grad_sample.expand(
                                self.model._module.LayerNorm.weight.grad_sample.shape[0], -1, -1)
                        )
                    self.opt.step()
                    self.opt.zero_grad()
                    self.model.zero_grad()
                    # update ema
                    for rate, params in zip(self.ema_rate, self.ema_params):
                        update_ema(params, self.master_params, rate=rate)

                    if self.step > 0 and self.step % self.save_interval == 0:
                        self.save()
                        # Run for a finite amount of time in integration tests.
                        if os.environ.get("DIFFUSION_TRAINING_TEST", "") and self.step > 0:
                            return

                    self.step += 1
                    pbar.update(1)

            # Save the last checkpoint if it wasn't already saved.
            if (self.step - 1) % self.save_interval != 0:
                self.save()

    def save(self):
        def save_checkpoint(rate, params):
            state_dict = self._master_params_to_state_dict(params)
            logger.log(f"saving model ...")

            filepath = os.path.join(self.args.checkpoint_path, f"{self.step + self.resume_step:06d}.pt")
            th.save(state_dict, filepath)  # save locally

        # save_checkpoint(0, self.master_params)
        for rate, params in zip(self.ema_rate, self.ema_params):
            save_checkpoint(rate, params)

    def _master_params_to_state_dict(self, master_params):
        if self.args.private:
            state_dict = self.model._module.state_dict()
            named_params = self.model._module.named_parameters()
        else:
            state_dict = self.model.state_dict()
            named_params = self.model.named_parameters()
        for i, (name, _value) in enumerate(named_params):
            assert name in state_dict
            state_dict[name] = master_params[i]
        return state_dict

    def _state_dict_to_master_params(self, state_dict):
        named_params = self.model._module.named_parameters() if self.args.private else self.model.named_parameters()
        params = [state_dict[name] for name, _ in named_params]
        return params


def parse_resume_step_from_filename(filename):
    """
    Parse filenames of the form path/to/modelNNNNNN.pt, where NNNNNN is the
    checkpoint's number of steps.
    """
    if filename[-3:] == '.pt':
        return int(filename[-9:-3])
    else:
        return 0


def get_blob_logdir():
    return os.environ.get("DIFFUSION_BLOB_LOGDIR", logger.get_dir())


def find_resume_checkpoint():
    # On your infrastructure, you may want to override this to automatically
    # discover the latest checkpoint on your blob storage, etc.
    return None


def find_ema_checkpoint(main_checkpoint, step, rate):
    if main_checkpoint is None:
        return None
    filename = f"ema_{rate}_{(step):06d}.pt"
    path = bf.join(bf.dirname(main_checkpoint), filename)
    if bf.exists(path):
        return path
    return None


def log_loss_dict(diffusion, ts, losses):
    for key, values in losses.items():
        logger.logkv_mean(key, values.mean().item())
        # Log the quantiles (four quartiles, in particular).
        for sub_t, sub_loss in zip(ts.cpu().numpy(), values.detach().cpu().numpy()):
            quartile = int(4 * sub_t / diffusion.num_timesteps)
            logger.logkv_mean(f"{key}_q{quartile}", sub_loss)


def actual_model_path(model_path):
    return model_path
