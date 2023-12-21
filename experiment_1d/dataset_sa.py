import numpy as np
import matplotlib.pyplot as plt
import torch as th
from torchvision import datasets,transforms
from util import gp_sampler,periodic_step,get_torch_size_string

def mnist(root_path='./data/',batch_size=128):
    """ 
        MNIST
    """
    mnist_train = datasets.MNIST(root=root_path,train=True,transform=transforms.ToTensor(),download=True)
    mnist_test  = datasets.MNIST(root=root_path,train=False,transform=transforms.ToTensor(),download=True)
    train_iter  = th.utils.data.DataLoader(mnist_train,batch_size=batch_size,shuffle=True,num_workers=1)
    test_iter   = th.utils.data.DataLoader(mnist_test,batch_size=batch_size,shuffle=True,num_workers=1)
    # Data
    train_data,train_label = mnist_train.data,mnist_train.targets
    test_data,test_label = mnist_test.data,mnist_test.targets
    return train_iter,test_iter,train_data,train_label,test_data,test_label

def get_1d_training_data(
    traj_type = 'step', # {'step','gp'}
    n_traj    = 10,
    L         = 100,
    device    = 'cpu',
    seed      = 1,
    plot_data = True,
    figsize   = (6,2),
    ls        = '-',
    lc        = 'k',
    lw        = 1,
    verbose   = True,
    split=0,
    varying=False,
    ):
    """ 
        1-D training data
    """
    hyp_lens = None
    idx_label = None
    traj_split = None
    if seed is not None:
        np.random.seed(seed=seed)
    times = np.linspace(start=0.0,stop=1.0,num=L).reshape((-1,1)) # [L x 1]
    if traj_type == 'gp':
        traj = th.from_numpy(
            gp_sampler(
                times    = times,
                hyp_gain = 2.0,
                hyp_len  = 0.2,
                meas_std = 1e-8,
                n_traj   = n_traj
            )
        ).to(th.float32).to(device) # [n_traj x L]
        if split > 0 :
            idx_arr = np.arange(L)
            idx_split = np.split(idx_arr,split)
            traj_split = np.split(traj.cpu().numpy(), split, axis=1)
            traj_split = np.vstack(traj_split)
            traj_split = th.from_numpy(traj_split).to(th.float32).to(device)
            labels = []
            idx_label = 0
            for i in idx_split :
                labels += [idx_label]*i.shape[0]
                idx_label += 1

            label_split = np.split(np.array(labels), split)
            label_split = np.vstack(label_split)
            label_split = th.from_numpy(label_split).to(th.long).to(device)
            labels = label_split
            # labels = th.from_numpy(np.array(labels)).to(th.long).to(device)
            
    elif traj_type == 'gp2':
        traj_np = np.zeros((n_traj,L))
        hyp_len_np = np.zeros((n_traj,1))
        hyp_len_candidate = [0.001, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0]
        for i_idx in range(n_traj):
            rand_idx = np.random.randint(0,7)
            traj_np[i_idx,:] = gp_sampler(
                times    = times,
                hyp_gain = 2.0,
                hyp_len  = hyp_len_candidate[rand_idx],
                meas_std = 1e-8,
                n_traj   = 1
            ).reshape(-1)
            hyp_len_np[i_idx] =  hyp_len_candidate[rand_idx]
        traj = th.from_numpy(
            traj_np
        ).to(th.float32).to(device) # [n_traj x L]
        hyp_lens = th.from_numpy(
            hyp_len_np
        ).to(th.float32).to(device)
        if split > 0 :
            idx_arr = np.arange(L)
            idx_split = np.split(idx_arr,split)
            traj_split = np.split(traj.cpu().numpy(), split, axis=1)
            traj_split = np.vstack(traj_split)
            traj_split = th.from_numpy(traj_split).to(th.float32).to(device)
            labels = []
            idx_label = 0
            for i in idx_split :
                labels += [idx_label]*i.shape[0]
                idx_label += 1
            labels = th.from_numpy(np.array(labels)).to(th.long).to(device)
    
    elif traj_type == 'step':
        traj_np = np.zeros((n_traj,L))
        for i_idx in range(n_traj):
            period      = np.random.uniform(low=0.38,high=0.42)
            time_offset = np.random.uniform(low=-0.02,high=0.02)
            y_min       = np.random.uniform(low=-3.2,high=-2.8)
            y_max       = np.random.uniform(low=2.8,high=3.2)
            traj_np[i_idx,:] = periodic_step(
                times       = times,
                period      = period,
                time_offset = time_offset,
                y_min       = y_min,
                y_max       = y_max
            ).reshape(-1)
        traj = th.from_numpy(
            traj_np
        ).to(th.float32).to(device) # [n_traj x L]
        if split > 0 :
            idx_arr = np.arange(L)
            idx_split = np.split(idx_arr,split)
            traj_split = np.split(traj.cpu().numpy(), split, axis=1)
            traj_split = np.vstack(traj_split)
            traj_split = th.from_numpy(traj_split).to(th.float32).to(device)
            labels = []
            idx_label = 0
            for i in idx_split :
                labels += [idx_label]*i.shape[0]
                idx_label += 1
            labels = th.from_numpy(np.array(labels)).to(th.long).to(device)
    
    elif traj_type == 'step2':
        traj_np = np.zeros((n_traj,L))
        for i_idx in range(n_traj): # for each trajectory
            # First, sample value and duration
            rate = 5
            val = np.random.uniform(low=-3.0,high=3.0)
            dur_tick = int(L*np.random.exponential(scale=1/rate))
            dim_dur  = 0.1 # minimum duration in sec
            dur_tick = max(dur_tick,int(dim_dur*L))
            
            tick_fr = 0
            tick_to = tick_fr+dur_tick
            while True:
                # Append
                traj_np[i_idx,tick_fr:min(L,tick_to)] = val
                
                # Termination condition
                if tick_to >= L: break 
                
                # sample value and duration
                val = np.random.uniform(low=-3.0,high=3.0)
                dur_tick = int(L*np.random.exponential(scale=1/rate))
                dur_tick = max(dur_tick,int(dim_dur*L))
                
                # Update tick
                tick_fr = tick_to
                tick_to = tick_fr+dur_tick
        traj = th.from_numpy(
            traj_np
        ).to(th.float32).to(device) # [n_traj x L]
        if split > 0 :
            idx_arr = np.arange(L)
            idx_split = np.split(idx_arr,split)
            traj_split = np.split(traj.cpu().numpy(), split, axis=1)
            traj_split = np.vstack(traj_split)
            traj_split = th.from_numpy(traj_split).to(th.float32).to(device)
            labels = []
            idx_label = 0
            for i in idx_split :
                labels += [idx_label]*i.shape[0]
                idx_label += 1
            labels = th.from_numpy(np.array(labels)).to(th.long).to(device)

    elif traj_type == 'triangle':
        traj_np = np.zeros((n_traj,L))
        for i_idx in range(n_traj):
            period      = 0.2
            time_offset = np.random.uniform(low=-0.02,high=0.02)
            y_min       = np.random.uniform(low=-3.2,high=-2.8)
            y_max       = np.random.uniform(low=2.8,high=3.2)
            times_mod = np.mod(times+time_offset,period)/period
            y = (y_max - y_min) * times_mod + y_min
            traj_np[i_idx,:] = y.reshape(-1)
        traj = th.from_numpy(
            traj_np
        ).to(th.float32).to(device) # [n_traj x L]
        if split > 0 :
            idx_arr = np.arange(L)
            idx_split = np.split(idx_arr,split)
            traj_split = np.split(traj.cpu().numpy(), split, axis=1)
            traj_split = np.vstack(traj_split)
            traj_split = th.from_numpy(traj_split).to(th.float32).to(device)
            labels = []
            idx_label = 0
            for i in idx_split :
                labels += [idx_label]*i.shape[0]
                idx_label += 1
            labels = th.from_numpy(np.array(labels)).to(th.long).to(device)

    else:
        print ("Unknown traj_type:[%s]"%(traj_type))
    # Plot
    if plot_data:
        plt.figure(figsize=figsize)
        for i_idx in range(n_traj): 
            plt.plot(times,traj[i_idx,:].cpu().numpy(),ls=ls,color=lc,lw=lw)
        plt.xlim([0.0,1.0])
        plt.ylim([-4,+4])
        plt.xlabel('Time',fontsize=10)
        plt.title('Trajectory type:[%s]'%(traj_type),fontsize=10)
        plt.show()
    # Print
    x_0 = traj[:,None,:] # [N x C x L]
    if verbose:
        print ("x_0:[%s]"%(get_torch_size_string(x_0)))
    # Out
    return times,x_0, hyp_lens, traj_split, labels