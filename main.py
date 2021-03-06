import copy
import glob
import os
import time
import sys

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from arguments import get_args
from baselines.common.vec_env.dummy_vec_env import DummyVecEnv
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from baselines.common.vec_env.vec_normalize import VecNormalize
from envs import make_env
from model import Policy
from storage import RolloutStorage
from utils import update_current_obs
from visualize import visdom_plot

import algo

from saturation import *
import pandas as pd

args = get_args()
R_ts = []

assert args.algo in ['a2c', 'ppo', 'acktr']
if args.recurrent_policy:
    assert args.algo in ['a2c', 'ppo'], \
        'Recurrent policy is not implemented for ACKTR'

num_updates = int(args.num_frames) // args.num_steps // args.num_processes

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

try:
    os.makedirs(args.log_dir)
except OSError:
    files = glob.glob(os.path.join(args.log_dir, '*'))
    for f in files:
        os.remove(f)


def main():
    print("#######")
    print("WARNING: All rewards are clipped or normalized so you need to use a monitor (see envs.py) or visdom plot to get true rewards")
    print("#######")

    torch.set_num_threads(1)

    if args.vis:
        from visdom import Visdom
        viz = Visdom(port=args.port)
        win = None

    envs = [make_env(args.env_name, args.seed, i, args.log_dir, args.add_timestep)
                for i in range(args.num_processes)]

    if args.num_processes > 1:
        envs = SubprocVecEnv(envs)
    else:
        envs = DummyVecEnv(envs)

    if len(envs.observation_space.shape) == 1:
        envs = VecNormalize(envs, gamma=args.gamma)

    obs_shape = envs.observation_space.shape
    obs_shape = (obs_shape[0] * args.num_stack, *obs_shape[1:])


    if args.load_model is not None:
        actor_critic = torch.load(args.load_model)[0]
    else:
        actor_critic = Policy(obs_shape, envs.action_space, args.recurrent_policy,
                               args.hidden_size, args)

    if envs.action_space.__class__.__name__ == "Discrete":
        action_shape = 1
    else:
        action_shape = envs.action_space.shape[0]

    if args.cuda:
        actor_critic.cuda()

    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, lr=args.lr,
                               eps=args.eps, alpha=args.alpha,
                               max_grad_norm=args.max_grad_norm,
                               pop_art=args.pop_art)
    elif args.algo == 'ppo':
        agent = algo.PPO(actor_critic, args.clip_param, args.ppo_epoch, args.num_mini_batch,
                         args.value_loss_coef, args.entropy_coef, lr=args.lr,
                               eps=args.eps,
                               max_grad_norm=args.max_grad_norm)
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, acktr=True)

    rollouts = RolloutStorage(args.num_steps, args.num_processes, obs_shape, envs.action_space, actor_critic.state_size)
    current_obs = torch.zeros(args.num_processes, *obs_shape)

    obs = envs.reset()
    update_current_obs(obs, current_obs, obs_shape, args.num_stack)

    rollouts.observations[0].copy_(current_obs)

    # These variables are used to compute average rewards for all processes.
    episode_rewards = torch.zeros([args.num_processes, 1])
    final_rewards = torch.zeros([args.num_processes, 1])

    if args.cuda:
        current_obs = current_obs.cuda()
        rollouts.cuda()

    start = time.time()
    scale = 1.
    current_pdrr = [0., 0.]
    last_update = 0

    ### parameters for adaptive reward scaling ###
    t_stop = 0
    beta = .99
    R_prev = -1e9
    m_max = -1e9
    m_t = 0
    reverse = False

    last_scale_t = -1e9
    ###


    for j in range(num_updates):
        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value, action, action_log_prob, states = actor_critic.act(
                        rollouts.observations[step],
                        rollouts.states[step],
                        rollouts.masks[step])
            cpu_actions = action.squeeze(1).cpu().numpy()

            # Obser reward and next obs
            obs, reward, done, info = envs.step(cpu_actions)

            # reward *= args.reward_scaling

            reward = torch.from_numpy(np.expand_dims(np.stack(reward), 1)).float()
            episode_rewards += reward

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            final_rewards *= masks
            final_rewards += (1 - masks) * episode_rewards
            episode_rewards *= masks

            if args.cuda:
                masks = masks.cuda()

            if current_obs.dim() == 4:
                current_obs *= masks.unsqueeze(2).unsqueeze(2)
            else:
                current_obs *= masks

            update_current_obs(obs, current_obs, obs_shape, args.num_stack)
            rollouts.insert(current_obs, states, action, action_log_prob, value, reward, masks)

        with torch.no_grad():
            next_value = actor_critic.get_value(rollouts.observations[-1],
                                                rollouts.states[-1],
                                                rollouts.masks[-1]).detach()

        rollouts.compute_returns(next_value, args.use_gae, args.gamma, args.tau)

        t = j // args.adaptive_interval
        if args.pop_art:
            value_loss, action_loss, dist_entropy = agent.pop_art_update(rollouts)
        else:
            if t - last_scale_t > 100:
                value_loss, action_loss, dist_entropy = agent.update(rollouts, update_actor=True)
            else:
                value_loss, action_loss, dist_entropy = agent.update(rollouts, update_actor=False)
        
        if agent.max_grad_norm < .5 and t - last_scale_t < 100:
            agent.max_grad_norm += 0.00001

        if  j % args.adaptive_interval == 0 and j and t - last_scale_t > 100:
            t = j // args.adaptive_interval

            R_t = float('{}'.format(final_rewards.mean()))
            R_ts.append(R_t)
            assert type(R_t) == float
            t_stop += 1
            m_t = beta * m_t + (1-beta) * R_t
            m_hat = m_t / (1-beta ** t)
            print('m_hat :{}, t_stop: {}'.format(m_hat, t_stop))
            print('agent.max_grad_norm, ', agent.max_grad_norm)
            if m_hat > m_max:
                m_max = m_hat
                t_stop = 0
            if t_stop > args.tolerance:
                if reverse and m_max <= R_prev:
                    break
                elif reverse and m_max > R_prev:
                    agent.max_grad_norm = args.max_grad_norm_after
                    actor_critic.rescale(args.cdec)
                    scale *= args.cdec
                    agent.reinitialize()
                    last_scale_t = t
                elif not reverse and m_max <= R_prev:
                    agent.max_grad_norm = args.max_grad_norm_after
                    actor_critic.rescale(args.cdec)
                    scale *= args.cdec
                    agent.reinitialize()
                    reverse = True
                    last_scale_t = t
                else:
                    agent.max_grad_norm = args.max_grad_norm_after
                    actor_critic.rescale(args.cinc)
                    scale *= args.cinc
                    agent.reinitialize()
                    last_scale_t = t

                R_prev = m_max
                j = t_stop = m_t =  0
                m_max = -1e9
            

        # if j % args.log_interval == 0:
            # this is used for testing saturation
            # relus = actor_critic.base_forward(
                    # rollouts.observations[:-1].view(-1, *rollouts.observations.size()[2:]))

        rollouts.after_update()

        if j % args.log_interval == 0:
            end = time.time()
            total_num_steps = (j + 1) * args.num_processes * args.num_steps


            # relus = log_saturation(fname=args.saturation_log,
                           # first=(j==0),
                           # relus=[relu.cpu().detach().numpy() for relu in relus])

            # print("saturation", relus)
            # if j > 0:
                # current_pdrr = incremental_update(current_pdrr, relus)

            print("Updates {}, num timesteps {}, FPS {}, mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}, entropy {:.5f}, value loss {:.5f}, policy loss {:.5f}, scale {:.5f}".
                format(j, total_num_steps,
                       int(total_num_steps / (end - start)),
                       final_rewards.mean(),
                       final_rewards.median(),
                       final_rewards.min(),
                       final_rewards.max(), dist_entropy,
                       value_loss, action_loss, scale))
            
                           
        if args.vis and j % args.vis_interval == 0:
            try:
                # Sometimes monitor doesn't properly flush the outputs
                win = visdom_plot(viz, win, args.log_dir, args.plot_title,
                                  args.algo, args.num_frames)
            except IOError:
                pass
    

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted')
        try:
            print('here')
            df = pd.DataFrame()
            df['R_t'] = np.array(R_ts)
            df.to_csv('{}/R_t.csv'.format(args.log_dir), index=False)
            sys.exit(0)
        except SystemExit:
            os._exit(0)
