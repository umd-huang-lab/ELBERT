from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import copy
import os
import random
import shutil
from pathlib import Path
import json

import numpy as np
import torch
from stable_baselines3.common.callbacks import CheckpointCallback
from stable_baselines3.common.logger import configure

import sys
sys.path.append('..')
### environment specific
from lending_experiment.config import CLUSTER_PROBABILITIES, GROUP_0_PROB, BANK_STARTING_CASH, INTEREST_RATE, CLUSTER_SHIFT_INCREMENT, \
    EXP_DIR, POLICY_KWARGS_fair, SAVE_FREQ, EVAL_INTERVAL, EP_TIMESTEPS_EVAL, EP_TIMESTEPS, EVAL_NUM_EPS
from lending_experiment.environments.lending import DelayedImpactEnv
from lending_experiment.environments.lending_params import DelayedImpactParams, two_group_credit_clusters
from lending_experiment.environments.rewards import LendingReward
from lending_experiment.agents.ppo.ppo_wrapper_env_fair import PPOEnvWrapper_fair
# plot evaluation
from lending_experiment.plot import plot_return_bias
# harder env
from lending_experiment.new_env import create_GeneralDelayedImpactEnv

### general to all environment (sb3)
from sb3_ppo_fair.ppo_fair import PPO_fair
from sb3_ppo_fair.policies_fair import ActorCriticPolicy_fair
from sb3_ppo_fair.utils_fair import DummyVecEnv_fair, Monitor_fair




device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print('Using device: ', device)
torch.cuda.empty_cache()

def parser_train():
    parser = argparse.ArgumentParser()

    # our method param
    parser.add_argument('--bias_coef', type=float, default=200000) 
    parser.add_argument('--beta_smooth', type=float, default=20) # not used since num_group = 2
    # baseline param
    parser.add_argument('--algorithm', type=str, default='ELBERT', choices=['ELBERT','APPO','GPPO','RPPO']) 
    parser.add_argument('--omega_APPO', type=float, default=0.005) # NOTE: this is hardwired in the reward.py
    parser.add_argument('--beta_0_APPO', type=float, default=1) 
    parser.add_argument('--beta_1_APPO', type=float, default=0.25) 
    parser.add_argument('--beta_2_APPO', type=float, default=0.25) 
    # training param
    parser.add_argument('--lr', type=float, default=1e-5) 
    parser.add_argument('--train_timesteps', type=int, default=1e7) # 5e6
    parser.add_argument('--buffer_size_training', type=int, default=4096)  # only for training; for evaluation, the buffer_size = env.ep_timesteps, the number of steps in one episode
    parser.add_argument('--exp_index', type=int, default=0)
    # base env param
    parser.add_argument('--modifedEnv', action='store_true') # If True, use harder modifed env
    # env param for wrapper and reward
    parser.add_argument('--include_delta', action='store_false', help='whether include the ratio in the observation space')
    parser.add_argument('--zeta_0', type=float, default=1) 
    parser.add_argument('--zeta_1', type=float, default=0) # for training (during eval zeta_1 = 0 always). Non-zero for RPPO (zeta_1=2). 
    # dir name
    parser.add_argument('--exp_path_env', type=str, default=None) # name of env

    # for debugging
    parser.add_argument('--main_reward_coef', type=float, default=1) # objective is maximizing main_reward_coef * main_reward - bias_coef * bias^2

    args = parser.parse_args()
    return args

def organize_param(args):
    '''
    organize the input arguments into groups
    '''
    # check method consistency
    if args.algorithm == 'APPO' or args.algorithm == 'GPPO':
        args.bias_coef = 0 # disable our method
        args.zeta_1 = 0 # disable RPPO
        args.main_reward_coef = 1
    elif args.algorithm == 'ELBERT':
        assert args.bias_coef > -1e-5, 'bias_coef should be positive when using our method'
        args.zeta_1 = 0 # disable RPPO
    else:
        # RPPO
        assert args.algorithm == 'RPPO', 'Invalid algorithm name. Should be among [ELBERT, APPO, GPPO, RPPO]'
        assert args.zeta_1 >  -1e-5, 'zeta_1 should be positive when using RPPO'
        args.bias_coef = 0 # disable our method
        args.main_reward_coef = 1

    if args.exp_path_env is None:
       args.exp_path_env = 'new_env' if args.modifedEnv else 'ori_env'

    print('\n\n\n',args,'\n\n\n')
    # our method param
    mitigation_params = {'bias_coef':args.bias_coef, 'beta_smooth':args.beta_smooth, \
                         'main_reward_coef':args.main_reward_coef}

    # baseline param
    baselines_params = {'method':args.algorithm, 'APPO': args.algorithm == 'APPO', 'OMEGA_APPO': args.omega_APPO, \
                        'BETA_0_APPO':args.beta_0_APPO, 'BETA_1_APPO':args.beta_1_APPO, 'BETA_2_APPO':args.beta_2_APPO}

    # base env param
    env_param_base = {'modifedEnv':args.modifedEnv,
                      'CLUSTER_PROBABILITIES':CLUSTER_PROBABILITIES, 'GROUP_0_PROB':GROUP_0_PROB, 'BANK_STARTING_CASH':BANK_STARTING_CASH,
                      'INTEREST_RATE':INTEREST_RATE, 'CLUSTER_SHIFT_INCREMENT':CLUSTER_SHIFT_INCREMENT}
    # env param for wrapper and reward
    env_param_dict_train = {'include_delta':args.include_delta, 'zeta_0':args.zeta_0, 'zeta_1':args.zeta_1, \
                      'ep_timesteps':EP_TIMESTEPS}
    env_param_dict_eval = {'include_delta':args.include_delta, 'zeta_0':args.zeta_0, 'zeta_1':0, \
                      'ep_timesteps':EP_TIMESTEPS_EVAL}
    
    # training param
    training_params = {'lr': args.lr, 'train_timesteps':args.train_timesteps, 'buffer_size_training':args.buffer_size_training}

    # evaluation param
    exp_dir  = get_dir(args)
    eval_kwargs = {'eval_write_path': exp_dir, \
                   'eval_interval':EVAL_INTERVAL, 'num_eps_eval':EVAL_NUM_EPS}
    
    # save args into file
    with open(os.path.join(exp_dir,'params.json'), 'w') as fp:
        for dict_ in [mitigation_params,baselines_params,env_param_base,env_param_dict_train,training_params,eval_kwargs]:
            json.dump(dict_, fp, sort_keys=False, indent=4)

    return mitigation_params, baselines_params, env_param_base, env_param_dict_train, env_param_dict_eval, training_params, eval_kwargs

def get_dir(args):
    '''
    name the experiment directory according to args
    '''
    print('args.exp_path_env :{}'.format(args.exp_path_env))
    exp_dir  = os.path.join(EXP_DIR, args.exp_path_env, args.algorithm)

    if args.algorithm == 'ELBERT':
        if args.main_reward_coef == 1:
            exp_dir  = os.path.join(exp_dir, 'alpha_{}'.format(args.bias_coef)+'lr_{}_'.format(args.lr)+'expindex_{}'.format(args.exp_index))
            print('Using ELBERT with bias_coef={}'.format(args.bias_coef))
        else:
            exp_dir  = os.path.join(exp_dir, 'MainCoef_{}'.format(args.main_reward_coef), \
                                'alpha_{}'.format(args.bias_coef)+'lr_{}_'.format(args.lr)+'expindex_{}'.format(args.exp_index))
            print('Using ELBERT with MainCoef={}, bias_coef={}'.format(args.main_reward_coef, args.bias_coef))
    else:
        exp_dir  = os.path.join(exp_dir, 'lr_{}_'.format(args.lr)+'expindex_{}'.format(args.exp_index))
        print('Using {}'.format(args.algorithm))
    
    if os.path.isdir(exp_dir):
        if 'debug' not in exp_dir:
            raise ValueError(f'{exp_dir} already exists; You could delete it manually if you want to train again')
        else:
            print(f'{exp_dir} already exists! You are in debug mode so this file will be overwritten \n\n')

    shutil.rmtree(exp_dir, ignore_errors=True) # clear the file first
    save_dir = f'{exp_dir}/models/'
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    
    return exp_dir

def train(env, mitigation_params, baselines_params, env_param_dict_train, env_param_dict_eval, training_params, eval_kwargs):

    env_train = PPOEnvWrapper_fair(env=copy.deepcopy(env), reward_fn=LendingReward, env_param_dict = env_param_dict_train)
    env_train = Monitor_fair(env_train)
    env_train = DummyVecEnv_fair([lambda: env_train]) 

    env_eval = PPOEnvWrapper_fair(env=copy.deepcopy(env), reward_fn=LendingReward, env_param_dict = env_param_dict_eval)
    eval_kwargs['env_eval'] = env_eval
   
    model = PPO_fair(ActorCriticPolicy_fair, env_train,
                policy_kwargs=POLICY_KWARGS_fair,
                verbose=1,
                learning_rate = training_params['lr'],
                n_steps = training_params['buffer_size_training'], 
                device=device,

                mitigation_params = mitigation_params,
                baselines_params = baselines_params, 
                eval_kwargs = eval_kwargs,
                )

    exp_dir = eval_kwargs['eval_write_path']
    save_dir = f'{exp_dir}/models/'

    checkpoint_callback = CheckpointCallback(save_freq=SAVE_FREQ, save_path=save_dir,
                                             name_prefix='rl_model')
    model.set_logger(configure(folder=exp_dir))

    model.learn(total_timesteps=training_params['train_timesteps'], callback=checkpoint_callback) # actual training
    model.save(save_dir + '/final_model')


def main():
    args = parser_train()

    mitigation_params, baselines_params, env_param_base, env_param_dict_train, env_param_dict_eval, training_params, eval_kwargs = \
    organize_param(args)
    
    if not args.modifedEnv:
        print('Using the original env')
        env_params = DelayedImpactParams(
            applicant_distribution=two_group_credit_clusters(
                cluster_probabilities=env_param_base['CLUSTER_PROBABILITIES'],
                group_likelihoods=[env_param_base['GROUP_0_PROB'], 1 - env_param_base['GROUP_0_PROB']]),
            bank_starting_cash=env_param_base['BANK_STARTING_CASH'],
            interest_rate=env_param_base['INTEREST_RATE'],
            cluster_shift_increment=env_param_base['CLUSTER_SHIFT_INCREMENT'],
        )

        env = DelayedImpactEnv(env_params)
    else:
        print('main.py: Using harder modified env')
        env = create_GeneralDelayedImpactEnv()

    train(env = env, mitigation_params = mitigation_params, baselines_params = baselines_params, env_param_dict_train = env_param_dict_train, \
          env_param_dict_eval = env_param_dict_eval, training_params = training_params, eval_kwargs = eval_kwargs)

    # plot evaluation
    plot_return_bias(eval_kwargs['eval_write_path'], smooth=2)


if __name__ == '__main__':
    main()