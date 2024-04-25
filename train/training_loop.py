import copy
import functools
import os
import time
from types import SimpleNamespace
import numpy as np

import blobfile as bf
import torch
from torch.optim import AdamW

from diffusion import logger
from utils import dist_util
from diffusion.fp16_util import MixedPrecisionTrainer
from diffusion.resample import LossAwareSampler, UniformSampler
from tqdm import tqdm
from diffusion.resample import create_named_schedule_sampler
from data_loaders.humanml.networks.evaluator_wrapper import EvaluatorMDMWrapper
from eval import eval_humanml, eval_humanact12_uestc
from data_loaders.get_data import get_dataset_loader


from data_loaders.humanml.utils.plot_script import plot_3d_motion
import data_loaders.humanml.utils.paramUtil as paramUtil

from data_loaders.humanml.scripts.motion_process import recover_from_ric
from data_loaders.tensors import collate

# For ImageNet experiments, this was a good default value.
# We found that the lg_loss_scale quickly climbed to
# 20-21 within the first ~1K steps of training.
INITIAL_LOG_LOSS_SCALE = 20.0


class TrainLoop:
    def __init__(self, args, train_platform, model, diffusion, data):
        self.args = args
        self.dataset = args.dataset
        self.train_platform = train_platform
        self.model = model
        self.diffusion = diffusion
        self.cond_mode = model.cond_mode
        self.data = data
        self.batch_size = args.batch_size
        self.microbatch = args.batch_size  # deprecating this option
        self.lr = args.lr
        self.log_interval = args.log_interval
        self.save_interval = args.save_interval
        self.resume_checkpoint = args.resume_checkpoint
        self.use_fp16 = False  # deprecating this option
        self.fp16_scale_growth = 1e-3  # deprecating this option
        self.weight_decay = args.weight_decay
        self.lr_anneal_steps = args.lr_anneal_steps

        self.step = 0
        self.resume_step = 0
        self.global_batch = self.batch_size # * dist.get_world_size()
        self.num_steps = args.num_steps
        self.num_epochs = self.num_steps // len(self.data) + 1

        self.sync_cuda = torch.cuda.is_available()

        self._load_and_sync_parameters()
        self.mp_trainer = MixedPrecisionTrainer(
            model=self.model,
            use_fp16=self.use_fp16,
            fp16_scale_growth=self.fp16_scale_growth,
        )

        self.save_dir = args.save_dir
        self.overwrite = args.overwrite

        self.opt = AdamW(
            self.mp_trainer.master_params, lr=self.lr, weight_decay=self.weight_decay
        )
        if self.resume_step:
            self._load_optimizer_state()
            # Model was resumed, either due to a restart or a checkpoint
            # being specified at the command line.

        self.device = torch.device("cpu")
        if torch.cuda.is_available() and dist_util.dev() != 'cpu':
            self.device = torch.device(dist_util.dev())

        self.schedule_sampler_type = 'uniform'
        self.schedule_sampler = create_named_schedule_sampler(self.schedule_sampler_type, diffusion)
        self.eval_wrapper, self.eval_data, self.eval_gt_data = None, None, None
        # if args.dataset in ['kit', 'humanml'] and args.eval_during_training:
        #     mm_num_samples = 0  # mm is super slow hence we won't run it during training
        #     mm_num_repeats = 0  # mm is super slow hence we won't run it during training
        #     gen_loader = get_dataset_loader(name=args.dataset, batch_size=args.eval_batch_size, num_frames=None,
        #                                     split=args.eval_split,
        #                                     hml_mode='eval')

        #     self.eval_gt_data = get_dataset_loader(name=args.dataset, batch_size=args.eval_batch_size, num_frames=None,
        #                                            split=args.eval_split,
        #                                            hml_mode='gt')
        #     self.eval_wrapper = EvaluatorMDMWrapper(args.dataset, dist_util.dev())
        #     self.eval_data = {
        #         'test': lambda: eval_humanml.get_mdm_loader(
        #             model, diffusion, args.eval_batch_size,
        #             gen_loader, mm_num_samples, mm_num_repeats, gen_loader.dataset.opt.max_motion_length,
        #             args.eval_num_samples, scale=1.,
        #         )
        #     }
        self.use_ddp = False
        self.ddp_model = self.model

    def _load_and_sync_parameters(self):
        resume_checkpoint = find_resume_checkpoint() or self.resume_checkpoint

        if resume_checkpoint:
            self.resume_step = parse_resume_step_from_filename(resume_checkpoint)
            logger.log(f"loading model from checkpoint: {resume_checkpoint}...")
            self.model.load_state_dict(
                dist_util.load_state_dict(
                    resume_checkpoint, map_location=dist_util.dev()
                )
            )

    def _load_optimizer_state(self):
        main_checkpoint = find_resume_checkpoint() or self.resume_checkpoint
        opt_checkpoint = bf.join(
            bf.dirname(main_checkpoint), f"opt{self.resume_step:09}.pt"
        )
        if bf.exists(opt_checkpoint):
            logger.log(f"loading optimizer state from checkpoint: {opt_checkpoint}")
            state_dict = dist_util.load_state_dict(
                opt_checkpoint, map_location=dist_util.dev()
            )
            self.opt.load_state_dict(state_dict)

    def run_loop(self):

        for epoch in range(self.num_epochs):
            print(f'Starting epoch {epoch}')
            for motion, cond in tqdm(self.data):
                if not (not self.lr_anneal_steps or self.step + self.resume_step < self.lr_anneal_steps):
                    break

                motion = motion.to(self.device)
                cond['y'] = {key: val.to(self.device) if torch.is_tensor(val) else val for key, val in cond['y'].items()}

                self.run_step(motion, cond)
                if self.step % self.log_interval == 0:
                    for k,v in logger.get_current().name2val.items():
                        if k == 'loss':
                            print('step[{}]: loss[{:0.5f}]'.format(self.step+self.resume_step, v))

                        if k in ['step', 'samples'] or '_q' in k:
                            continue
                        else:
                            self.train_platform.report_scalar(name=k, value=v, iteration=self.step, group_name='Loss')

                if self.step % self.save_interval == 0:
                    self.save()
                    self.model.eval()
                    self.evaluate()
                    self.model.train()

                    # Run for a finite amount of time in integration tests.
                    if os.environ.get("DIFFUSION_TRAINING_TEST", "") and self.step > 0:
                        return
                self.step += 1
            if not (not self.lr_anneal_steps or self.step + self.resume_step < self.lr_anneal_steps):
                break
        # Save the last checkpoint if it wasn't already saved.
        if (self.step - 1) % self.save_interval != 0:
            self.save()
            self.evaluate()


    def evaluate(self):
        if not self.args.eval_during_training:
            return
        start_eval = time.time()
        
        collate_args = [{'inp': torch.zeros(196), 'tokens': None, 'lengths': 196}] * 1
        texts = ['a man run fast']
        collate_args = [dict(arg, text=txt) for arg, txt in zip(collate_args, texts)]
        # collate_args = [{'inp': torch.zeros(60), 'tokens': None, 'lengths': 60}] * 1
        _, model_kwargs = collate(collate_args)


        eval_K_params = None
        eval_len_param = None

        self.model.eval()
        for i in range(2):
            all_motions = []
            all_lengths = []
            all_text = []
            sample_fn = self.diffusion.p_sample_loop
            sample = sample_fn(
                self.model,
                # (args.batch_size, model.njoints, model.nfeats, n_frames),  # BUG FIX - this one caused a mismatch between training and inference
                (1, self.model.njoints, self.model.nfeats, 196),  # BUG FIX
                None,
                None,
                clip_denoised=False,
                model_kwargs=model_kwargs,
                skip_timesteps=0,  # 0 is the default value - i.e. don't skip any step
                init_image=None,
                progress=True,
                dump_steps=None,
                noise=None,
                const_noise=False,
            )        
            
                    # Recover XYZ *positions* from HumanML3D vector representation
            n_joints = 22 if sample.shape[1] == 263 else 21
            sample = self.data.dataset.t2m_dataset.inv_transform(sample.cpu().permute(0, 2, 3, 1)).float()
            sample = recover_from_ric(sample, n_joints)
            sample = sample.view(-1, *sample.shape[2:]).permute(0, 2, 3, 1)

            rot2xyz_pose_rep = 'xyz' if  self.model.data_rep in ['xyz', 'hml_vec'] else self.model.data_rep
            rot2xyz_mask = None if rot2xyz_pose_rep == 'xyz' else model_kwargs['y']['mask'].reshape(1, 196).bool()
            sample = self.model.rot2xyz(x=sample, mask=rot2xyz_mask, pose_rep=rot2xyz_pose_rep, glob=True, translation=True,
                                jointstype='smpl', vertstrans=True, betas=None, beta=0, glob_rot=None,
                                get_rotations_back=False)

            text_key = 'text' if 'text' in model_kwargs['y'] else 'action_text'
            all_text += model_kwargs['y'][text_key]

            all_motions.append(sample.cpu().numpy())
            all_lengths.append(model_kwargs['y']['lengths'].cpu().numpy())
            all_motions = np.concatenate(all_motions, axis=0)
            all_motions = all_motions[:1]  # [bs, njoints, 6, seqlen]
            all_text = all_text[:1]
            all_lengths = np.concatenate(all_lengths, axis=0)[:1]

            skeleton = paramUtil.t2m_kinematic_chain
            motion = all_motions[0].transpose(2, 0, 1)
            if i == 0 :
                plot_3d_motion(self.save_dir+'/eval_result_fast_'+str(self.step)+'.mp4', skeleton, motion, dataset=self.args.dataset, title='length : 0.3', fps=20)
            else : 
                plot_3d_motion(self.save_dir+'/eval_result_slow_'+str(self.step)+'.mp4', skeleton, motion, dataset=self.args.dataset, title='length : 1.0', fps=20)
        
        self.model.train()
        # if self.eval_wrapper is not None:
        #     print('Running evaluation loop: [Should take about 90 min]')
        #     log_file = os.path.join(self.save_dir, f'eval_humanml_{(self.step + self.resume_step):09d}.log')
        #     diversity_times = 300
        #     mm_num_times = 0  # mm is super slow hence we won't run it during training
        #     eval_dict = eval_humanml.evaluation(
        #         self.eval_wrapper, self.eval_gt_data, self.eval_data, log_file,
        #         replication_times=self.args.eval_rep_times, diversity_times=diversity_times, mm_num_times=mm_num_times, run_mm=False)
        #     print(eval_dict)
        #     for k, v in eval_dict.items():
        #         if k.startswith('R_precision'):
        #             for i in range(len(v)):
        #                 self.train_platform.report_scalar(name=f'top{i + 1}_' + k, value=v[i],
        #                                                   iteration=self.step + self.resume_step,
        #                                                   group_name='Eval')
        #         else:
        #             self.train_platform.report_scalar(name=k, value=v, iteration=self.step + self.resume_step,
        #                                               group_name='Eval')

        # elif self.dataset in ['humanact12', 'uestc']:
        #     eval_args = SimpleNamespace(num_seeds=self.args.eval_rep_times, num_samples=self.args.eval_num_samples,
        #                                 batch_size=self.args.eval_batch_size, device=self.device, guidance_param = 1,
        #                                 dataset=self.dataset, unconstrained=self.args.unconstrained,
        #                                 model_path=os.path.join(self.save_dir, self.ckpt_file_name()))
        #     eval_dict = eval_humanact12_uestc.evaluate(eval_args, model=self.model, diffusion=self.diffusion, data=self.data.dataset)
        #     print(f'Evaluation results on {self.dataset}: {sorted(eval_dict["feats"].items())}')
        #     for k, v in eval_dict["feats"].items():
        #         if 'unconstrained' not in k:
        #             self.train_platform.report_scalar(name=k, value=np.array(v).astype(float).mean(), iteration=self.step, group_name='Eval')
        #         else:
        #             self.train_platform.report_scalar(name=k, value=np.array(v).astype(float).mean(), iteration=self.step, group_name='Eval Unconstrained')

        end_eval = time.time()
        print(f'Evaluation time: {round(end_eval-start_eval)/60}min')



    def run_step(self, batch, cond):
        self.forward_backward(batch, cond)
        self.mp_trainer.optimize(self.opt)
        self._anneal_lr()
        self.log_step()

    def forward_backward(self, batch, cond):
        self.mp_trainer.zero_grad()
        for i in range(0, batch.shape[0], self.microbatch):
            # Eliminates the microbatch feature
            assert i == 0
            assert self.microbatch == self.batch_size
            micro = batch
            micro_cond = cond
            last_batch = (i + self.microbatch) >= batch.shape[0]
            t, weights = self.schedule_sampler.sample(micro.shape[0], dist_util.dev())

            compute_losses = functools.partial(
                self.diffusion.training_losses,
                self.ddp_model,
                micro,  # [bs, ch, image_size, image_size]
                t,  # [bs](int) sampled timesteps
                K_params=None,
                len_param=None,
                model_kwargs=micro_cond,
                dataset=self.data.dataset
            )

            if last_batch or not self.use_ddp:
                losses = compute_losses()
            else:
                with self.ddp_model.no_sync():
                    losses = compute_losses()

            if isinstance(self.schedule_sampler, LossAwareSampler):
                self.schedule_sampler.update_with_local_losses(
                    t, losses["loss"].detach()
                )

            loss = (losses["loss"] * weights).mean()
            log_loss_dict(
                self.diffusion, t, {k: v * weights for k, v in losses.items()}
            )
            self.mp_trainer.backward(loss)


    def _anneal_lr(self):
        if not self.lr_anneal_steps:
            return
        frac_done = (self.step + self.resume_step) / self.lr_anneal_steps
        lr = self.lr * (1 - frac_done)
        for param_group in self.opt.param_groups:
            param_group["lr"] = lr

    def log_step(self):
        logger.logkv("step", self.step + self.resume_step)
        logger.logkv("samples", (self.step + self.resume_step + 1) * self.global_batch)


    def ckpt_file_name(self):
        return f"model{(self.step+self.resume_step):09d}.pt"


    def save(self):
        def save_checkpoint(params):
            state_dict = self.mp_trainer.master_params_to_state_dict(params)

            # Do not save CLIP weights
            clip_weights = [e for e in state_dict.keys() if e.startswith('clip_model.')]
            for e in clip_weights:
                del state_dict[e]

            logger.log(f"saving model...")
            filename = self.ckpt_file_name()
            with bf.BlobFile(bf.join(self.save_dir, filename), "wb") as f:
                torch.save(state_dict, f)

        save_checkpoint(self.mp_trainer.master_params)

        with bf.BlobFile(
            bf.join(self.save_dir, f"opt{(self.step+self.resume_step):09d}.pt"),
            "wb",
        ) as f:
            torch.save(self.opt.state_dict(), f)


def parse_resume_step_from_filename(filename):
    """
    Parse filenames of the form path/to/modelNNNNNN.pt, where NNNNNN is the
    checkpoint's number of steps.
    """
    split = filename.split("model")
    if len(split) < 2:
        return 0
    split1 = split[-1].split(".")[0]
    try:
        return int(split1)
    except ValueError:
        return 0


def get_blob_logdir():
    # You can change this to be a separate path to save checkpoints to
    # a blobstore or some external drive.
    return logger.get_dir()


def find_resume_checkpoint():
    # On your infrastructure, you may want to override this to automatically
    # discover the latest checkpoint on your blob storage, etc.
    return None


def log_loss_dict(diffusion, ts, losses):
    for key, values in losses.items():
        logger.logkv_mean(key, values.mean().item())
        # Log the quantiles (four quartiles, in particular).
        for sub_t, sub_loss in zip(ts.cpu().numpy(), values.detach().cpu().numpy()):
            quartile = int(4 * sub_t / diffusion.num_timesteps)
            logger.logkv_mean(f"{key}_q{quartile}", sub_loss)
