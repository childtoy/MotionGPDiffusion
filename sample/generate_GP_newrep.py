# This code is based on https://github.com/openai/guided-diffusion
"""
Generate a large batch of image samples from a model and save them as a large
numpy array. This can be used to produce samples for FID evaluation.
"""
from utils.fixseed import fixseed
import os
import numpy as np
import torch
from utils.parser_util import generate_args
from utils.model_util import create_model_and_diffusion, load_model_wo_clip
from utils import dist_util
from model.cfg_sampler import ClassifierFreeSampleModel
from data_loaders.get_data import get_dataset_loader
from data_loaders.humanml.scripts.motion_process import recover_from_ric
import data_loaders.humanml.utils.paramUtil as paramUtil
from data_loaders.humanml.utils.plot_script import plot_3d_motion
import shutil
from data_loaders.tensors import collate
import pickle as pkl
from data_loaders.humanml.utils.paramUtil import t2m_kinematic_chain, t2m_raw_offsets
from data_loaders.humanml.utils.rotation_conversion import cont6d_to_matrix_np, cont6d_to_matrix,matrix_to_quaternion
from data_loaders.humanml.utils.skeleton import Skeleton, skel_joints

def main():
    args = generate_args()
    fixseed(args.seed)
    out_path = args.output_dir
    name = os.path.basename(os.path.dirname(args.model_path))
    niter = os.path.basename(args.model_path).replace('model', '').replace('.pt', '')
    max_frames = 196 if args.dataset in ['kit', 'humanml','humanml2'] else 60
    fps = 12.5 if args.dataset == 'kit' else 20
    n_frames = min(max_frames, int(args.motion_length*fps))
    is_using_data = not any([args.input_text, args.text_prompt, args.action_file, args.action_name])
    
    with open(args.param_lenK_path, 'rb') as f : 
        param_lenK = pkl.load(f)    
        num_len = len(param_lenK['K_param'])
        K_param = torch.Tensor(param_lenK['K_param']).to(args.device)
        K_template = param_lenK['template']
        K_template = torch.Tensor(K_template).repeat(10,1,1,1)

    dist_util.setup_dist(args.device)
    
    # this block must be called BEFORE the dataset is loaded
    if args.text_prompt != '':
        texts = [args.text_prompt]
        args.num_samples = 1
    elif args.input_text != '':
        assert os.path.exists(args.input_text)
        with open(args.input_text, 'r') as fr:
            texts = fr.readlines()
        texts = [s.replace('\n', '') for s in texts]
        args.num_samples = len(texts)
    elif args.action_name:
        action_text = [args.action_name]
        args.num_samples = 1
    elif args.action_file != '':
        assert os.path.exists(args.action_file)
        with open(args.action_file, 'r') as fr:
            action_text = fr.readlines()
        action_text = [s.replace('\n', '') for s in action_text]
        args.num_samples = len(action_text)

    assert args.num_samples <= args.batch_size, \
        f'Please either increase batch_size({args.batch_size}) or reduce num_samples({args.num_samples})'
    # So why do we need this check? In order to protect GPU from a memory overload in the following line.
    # If your GPU can handle batch size larger then default, you can specify it through --batch_size flag.
    # If it doesn't, and you still want to sample more prompts, run this script with different seeds
    # (specify through the --seed flag)
    args.batch_size = args.num_samples  # Sampling a single batch from the testset, with exactly args.num_samples

    print('Loading dataset...')
    data = load_dataset(args, max_frames, n_frames)
    total_num_samples = args.num_samples * args.num_repetitions

    print("Creating model and diffusion...")
    model, diffusion = create_model_and_diffusion(args, data)

    print(f"Loading checkpoints from [{args.model_path}]...")
    state_dict = torch.load(args.model_path, map_location='cpu')
    load_model_wo_clip(model, state_dict)

    if args.guidance_param != 1:
        model = ClassifierFreeSampleModel(model)   # wrapping model with the classifier-free sampler
    model.to(dist_util.dev())
    model.eval()  # disable random masking

    if is_using_data:
        iterator = iter(data)
        _, model_kwargs = next(iterator)
    else:
        collate_args = [{'inp': torch.zeros(n_frames), 'tokens': None, 'lengths': n_frames}] * args.num_samples
        is_t2m = any([args.input_text, args.text_prompt])
        if is_t2m:
            # t2m
            collate_args = [dict(arg, text=txt) for arg, txt in zip(collate_args, texts)]
        else:
            # a2m
            action = data.dataset.action_name_to_action(action_text)
            collate_args = [dict(arg, action=one_action, action_text=one_action_text) for
                            arg, one_action, one_action_text in zip(collate_args, action, action_text)]
        _, model_kwargs = collate(collate_args)
    
    print(model_kwargs)
    
    all_motions = []
    all_lengths = []
    all_text = []
    # lens_array =  np.array([0.03, 0.12, 0.21, 0.3, 0.39, 0.48, 0.57, 0.66,0.8, 1.0])
    # lens_str = ['003','012','021','030','039','048','057','066','080','100']
    # lens_array =  np.array([0.033     , 0.14044444, 
    #          0.24788889, 0.35533333, 
    #          0.46277778, 0.67766667, 
    #          1.        ])
    lens_array =  np.array([0.033     , 0.14044444, 
                            0.24788889, 0.35533333, 
                            0.46277778, 0.67766667, 
                            1.        ,])
                            # 0.08, 0.19, 0.30])
    # lens_array = np.array([0.033,
    #                        0.247888889,
    #                        0.462777778,
    #                        1.0])
    
    lens_str = ['003','014','024','035','046', '067', '100']#, '008', '019', '030']
    # lens_str = ['003','024','046','100']
    eval_K_params = torch.zeros((len(lens_array),263,196,196)).to(args.device) 
    eval_len_param = torch.ones((len(lens_array),263)).to(args.device) * 0.03
    for i in range(len(lens_array)):
        eval_K_params = K_template
        if args.corr_mode == 'all_trs': 
            eval_K_params[i,1:3] = torch.Tensor(K_param[i]).repeat(2,1,1)
            eval_len_param[i,1:3] = torch.Tensor([lens_array[i]]).to(args.device).repeat(2)
        elif args.corr_mode == 'all_trsrot':
            slices = [[0, 4], [193, 196]]
            eval_K_params[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor(K_param[i]).repeat(7,1,1)
            eval_len_param[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor([lens_array[i]]).to(args.device).repeat(7)
        elif args.corr_mode == 'R_trs':
            eval_K_params[i,1:3] = torch.Tensor(K_param[i]).repeat(2,1,1)
            eval_len_param[i,1:3] = torch.Tensor([lens_array[i]]).to(args.device).repeat(2)
        elif args.corr_mode == 'R_trsrot':
            slices = [[0, 4], [193, 196]]
            eval_K_params[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor(K_param[i]).repeat(7,1,1)
            eval_len_param[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor([lens_array[i]]).to(args.device).repeat(7)
        # eval_len_param[i] = torch.Tensor([lens_array[i]]).to(args.device).repeat(263)
        elif args.corr_mode == 'R_trsrot':
            slices = [[4, 67]]
            eval_K_params[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor(K_param[i]).repeat(7,1,1)
            eval_len_param[i,np.concatenate([np.arange(*k) for k in slices])] = torch.Tensor([lens_array[i]]).to(args.device).repeat(7)
        elif args.corr_mode == 'all':
            
        # else :
            assert('wrong corr_mode')
    model.eval()
    save_motion = []
    save_len_param = []

    n_raw_offsets = torch.from_numpy(t2m_raw_offsets)
    kinematic_chain = t2m_kinematic_chain
    skeleton = Skeleton(n_raw_offsets, kinematic_chain, args.device)


    for i in range(len(lens_array)):
        out_path = ''
        if out_path == '':
            out_path = os.path.join(os.path.dirname(args.model_path),args.text_prompt+'_cfg',
                                'samples_{}_{}_len{}_{}_seed{}'.format(args.corr_mode, name, lens_str[i], niter, args.seed))
        if args.text_prompt != '':
            out_path += '_' + args.text_prompt.replace(' ', '_').replace('.', '')
        elif args.input_text != '':
            out_path += '_' + os.path.basename(args.input_text).replace('.txt', '').replace(' ', '_').replace('.', '')

        # if os.path.exists(out_path):
        #     shutil.rmtree(out_path)
        os.makedirs(out_path, exist_ok=True)

        rep = args.num_repetitions

        for j in range(rep):
            all_motions = []
            all_lengths = []
            all_text = []
            
            if args.guidance_param != 1:
                model_kwargs['y']['scale'] = torch.ones(1, device=dist_util.dev()) * args.guidance_param

            num_samples = args.num_samples
                
            sample_fn = diffusion.p_sample_loop
            sample = sample_fn(
                model,
                (num_samples, model.njoints, model.nfeats, 196),  # BUG FIX
                eval_K_params[i].unsqueeze(0),
                eval_len_param[i].unsqueeze(0),
                clip_denoised=False,
                model_kwargs=model_kwargs,
                skip_timesteps=0,  # 0 is the default value - i.e. don't skip any step
                init_image=None,
                progress=True,
                dump_steps=None,
                noise=None,
                const_noise=False,
            )        
            
            motion = sample.squeeze().permute(0,2,1)
            rot6d, r_pos = recover_from_rot(motion, 22)
            rot6d = rot6d.reshape(r_pos.shape[0],r_pos.shape[1],22,6)
            n_batch, n_frames, n_joints = 1, 196, 22
            
            rotation_quat = matrix_to_quaternion(
                cont6d_to_matrix(rot6d)
                )
            print(rotation_quat)
            rotation_quat = rotation_quat.reshape(64, n_frames, n_joints, 4)

            # root_pos = translation
            local_q_normalized = torch.nn.functional.normalize(rotation_quat, p=2.0, dim=-1)
            # global_pos, global_q = skeleton_smpl.forward_kinematics_with_rotation5(local_q_normalized, r_pos)
            joint_rot = local_q_normalized.cpu().numpy().reshape(n_batch*n_frames,n_joints,4)
            root_trj = r_pos.cpu().numpy().reshape(n_batch*n_frames,3)
                    
            new_shape = (n_batch*n_frames, n_joints, 3)
            skel_joint = torch.Tensor(skel_joints).to(args.device).expand(new_shape)
            # print(rotation_quat)
            new_joints = skeleton.forward_kinematics(rotation_quat.reshape(n_batch*n_frames,n_joints,4), r_pos.reshape(n_batch*n_frames,3), skel_joints=skel_joint)
            
            new_joints = new_joints.reshape(n_batch, n_frames, n_joints, 3)
            sample = new_joints                    
                    
            text_key = 'text' if 'text' in model_kwargs['y'] else 'action_text'
            all_text += model_kwargs['y'][text_key]
            all_motions.append(sample.cpu().numpy())
            save_motion.append(sample.cpu().numpy())
            all_lengths.append(model_kwargs['y']['lengths'].cpu().numpy())
            all_motions = np.concatenate(all_motions, axis=0)
            # all_motions = all_motions[:1]  # [bs, njoints, 6, seqlen]
            all_text = all_text[:1]
            all_lengths = np.concatenate(all_lengths, axis=0)[:1]


            print('all_motions', len(all_motions)) # frames
            motion = all_motions[0].transpose(2, 0, 1)
            # if j < 5 : 
            plot_3d_motion(out_path, '/eval_result_lens_'+lens_str[i]+'_rep_'+str(j)+'.gif', t2m_kinematic_chain, motion, dataset=args.dataset, title='length :'+str(lens_array[i]), fps=20)
            
            if j == 0:
                motion = all_motions.transpose(0, 3, 1, 2)
                np.save(out_path+'/eval_result_lens_'+lens_str[i]+'_rep_'+str(j)+'.npy', motion)
            
            print('finised j:', j)
            
    abs_path = os.path.abspath(out_path)
    print(f'[Done] Results are at [{abs_path}]')

    with open(out_path+'/sampled_motion_lens.pkl', 'wb') as f :
        pkl.dump(save_motion, f)

def save_multiple_samples(args, out_path, row_print_template, all_print_template, row_file_template, all_file_template,
                          caption, num_samples_in_out_file, rep_files, sample_files, sample_i):
    all_rep_save_file = row_file_template.format(sample_i)
    all_rep_save_path = os.path.join(out_path, all_rep_save_file)
    ffmpeg_rep_files = [f' -i {f} ' for f in rep_files]
    hstack_args = f' -filter_complex hstack=inputs={args.num_repetitions}' if args.num_repetitions > 1 else ''
    ffmpeg_rep_cmd = f'ffmpeg -y -loglevel warning ' + ''.join(ffmpeg_rep_files) + f'{hstack_args} {all_rep_save_path}'
    os.system(ffmpeg_rep_cmd)
    print(row_print_template.format(caption, sample_i, all_rep_save_file))
    sample_files.append(all_rep_save_path)
    if (sample_i + 1) % num_samples_in_out_file == 0 or sample_i + 1 == args.num_samples:
        # all_sample_save_file =  f'samples_{(sample_i - len(sample_files) + 1):02d}_to_{sample_i:02d}.mp4'
        all_sample_save_file = all_file_template.format(sample_i - len(sample_files) + 1, sample_i)
        all_sample_save_path = os.path.join(out_path, all_sample_save_file)
        print(all_print_template.format(sample_i - len(sample_files) + 1, sample_i, all_sample_save_file))
        ffmpeg_rep_files = [f' -i {f} ' for f in sample_files]
        vstack_args = f' -filter_complex vstack=inputs={len(sample_files)}' if len(sample_files) > 1 else ''
        ffmpeg_rep_cmd = f'ffmpeg -y -loglevel warning ' + ''.join(
            ffmpeg_rep_files) + f'{vstack_args} {all_sample_save_path}'
        os.system(ffmpeg_rep_cmd)
        sample_files = []
    return sample_files


def construct_template_variables(unconstrained):
    row_file_template = 'sample{:02d}.gif'
    all_file_template = 'samples_{:02d}_to_{:02d}.gif'
    if unconstrained:
        sample_file_template = 'row{:02d}_col{:02d}.gif'
        sample_print_template = '[{} row #{:02d} column #{:02d} | -> {}]'
        row_file_template = row_file_template.replace('sample', 'row')
        row_print_template = '[{} row #{:02d} | all columns | -> {}]'
        all_file_template = all_file_template.replace('samples', 'rows')
        all_print_template = '[rows {:02d} to {:02d} | -> {}]'
    else:
        sample_file_template = 'sample{:02d}_rep{:02d}.gif'
        sample_print_template = '["{}" ({:02d}) | Rep #{:02d} | -> {}]'
        row_print_template = '[ "{}" ({:02d}) | all repetitions | -> {}]'
        all_print_template = '[samples {:02d} to {:02d} | all repetitions | -> {}]'

    return sample_print_template, row_print_template, all_print_template, \
           sample_file_template, row_file_template, all_file_template


def load_dataset(args, max_frames, n_frames):
    data = get_dataset_loader(name=args.dataset,
                              batch_size=args.batch_size,
                              num_frames=max_frames,
                              split='test',
                              hml_mode='text_only')
    if args.dataset in ['kit', 'humanml']:
        data.dataset.t2m_dataset.fixed_length = n_frames
    return data


if __name__ == "__main__":
    main()
