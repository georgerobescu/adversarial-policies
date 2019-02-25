from collections import defaultdict
import json
import os

import numpy as np
from stable_baselines import logger
from stable_baselines.common.base_class import BaseRLModel
from stable_baselines.common.vec_env import VecEnvWrapper

from modelfree import envs  # noqa - needed to register sumo_auto_contact
from modelfree.scheduling import LinearAnnealer


class RewardShapingVecWrapper(VecEnvWrapper):
    """A more direct interface for shaping the reward of the attacking agent."""
    def __init__(self, venv, agent_idx, shaping_params, batch_size, reward_annealer=None):
        super().__init__(venv)
        # structure: {'sparse': {k: v}, 'dense': {k: v}, **kwargs}
        self.shaping_params = shaping_params
        self.reward_annealer = reward_annealer
        self.counter = 0
        self.batch_size = batch_size
        self.agent_idx = agent_idx

        self.ep_rew_dict = defaultdict(list)
        self.step_rew_dict = defaultdict(lambda: [[] for _ in range(self.num_envs)])

    @classmethod
    def get_shaping_params(cls, shaping_params, env_name):
        path_stem = 'experiments/rew_configs/'
        default_configs = {
            'multicomp/SumoHumans-v0': 'default_hsumo.json',
            'multicomp/SumoHumansAutoContact-v0': 'default_hsumo.json'
        }
        assert env_name in default_configs, \
            "No default reward shaping config found for env_name:{}".format(env_name)
        fname = os.path.join(path_stem, default_configs[env_name])
        final_shaping_params = json.load(open(fname))

        if shaping_params is not None:
            config = json.load(open(shaping_params))
            final_shaping_params['sparse'].update(config['sparse'])
            final_shaping_params['dense'].update(config['dense'])
            final_shaping_params['rew_shape_anneal_frac'] = config.get('rew_shape_anneal_frac')
        return final_shaping_params

    def _log_sparse_dense_rewards(self):
        if self.counter == self.batch_size * self.num_envs:
            num_episodes = len(self.ep_rew_dict['sparse']['reward_remaining'])
            dense_terms = self.shaping_params['dense'].keys()
            for term in dense_terms:
                assert len(self.ep_rew_dict[term]) == num_episodes
            ep_dense_mean = sum([sum(self.ep_rew_dict[t]) for t in dense_terms]) / num_episodes
            logger.logkv('epdensemean', ep_dense_mean)

            sparse_terms = self.shaping_params['sparse'].keys()
            for term in sparse_terms:
                assert len(self.ep_rew_dict[term]) == num_episodes
            ep_sparse_mean = sum([sum(self.ep_rew_dict[t]) for t in sparse_terms]) / num_episodes
            logger.logkv('epsparsemean', ep_sparse_mean)

            c = self.reward_annealer()
            logger.logkv('rew_anneal', c)
            ep_rew_mean = c * ep_dense_mean + (1 - c) * ep_sparse_mean
            logger.logkv('eprewmean_true', ep_rew_mean)

            for rew_type in self.ep_rew_dict:
                self.ep_rew_dict[rew_type] = []
            self.counter = 0

    def reset(self):
        return self.venv.reset()

    def step_wait(self):
        obs, rew, done, infos = self.venv.step_wait()
        for env_num in range(self.num_envs):
            shaped_reward = 0
            for rew_term, rew_value in infos[env_num][self.agent_idx].items():
                # our shaping_params dictionary is hierarchically separated the rew_terms
                # into two rew_types, 'dense' and 'sparse'
                if rew_term in self.shaping_params['sparse']:
                    rew_type = 'sparse'
                elif rew_term in self.shaping_params['dense']:
                    rew_type = 'dense'
                else:
                    continue

                # weighted_reward := our weight for that term * value of that term
                weighted_reward = self.shaping_params[rew_type][rew_term] * rew_value
                self.step_rew_dict[rew_type][env_num].append(weighted_reward)

                # perform annealing if necessary and then accumulate total shaped_reward
                if self.reward_annealer is not None:
                    c = self.get_annealed_exploration_reward()
                    if rew_type == 'sparse':
                        weighted_reward *= (1 - c)
                    else:
                        weighted_reward *= c
                shaped_reward += weighted_reward

            # log the results of an episode into buffers and then pass on the shaped reward
            if done[env_num]:
                for rew_type in self.step_rew_dict:
                    self.ep_rew_dict[rew_type].append(sum(self.step_rew_dict[rew_type][env_num]))
                    self.step_rew_dict[rew_type][env_num] = []
            self.counter += 1
            self._log_sparse_dense_rewards()
            rew[env_num] = shaped_reward

        return obs, rew, done, infos

    def get_annealed_exploration_reward(self):
        """
        Returns c (which will be annealed to zero) s.t. the total reward equals
        c * reward_move + (1 - c) * reward_remaining
        """
        c = self.reward_annealer()
        assert 0 <= c <= 1
        return c


class NoisyAgentWrapper(BaseRLModel):
    def __init__(self, agent, noise_annealer, noise_type='gaussian'):
        """
        Wrap an agent and add noise to its actions
        :param agent: BaseRLModel the agent to wrap
        :param noise_annealer: Annealer.get_value - presumably the noise should be decreased
        over time in order to get the adversarial policy to perform well on a normal victim.
        :param noise_type: str - the type of noise parametrized by noise_annealer's value.
        Current options are [gaussian]
        """
        self.agent = agent
        self.sess = agent.sess
        self.noise_annealer = noise_annealer
        self.noise_generator = self._get_noise_generator(noise_type)

    @classmethod
    def get_noise_params(cls, noise_params, env_name):
        path_stem = 'experiments/noise_configs/'
        default_configs = {
            'multicomp/SumoHumans-v0': 'default_hsumo.json',
            'multicomp/SumoHumansAutoContact-v0': 'default_hsumo.json'
        }
        assert env_name in default_configs, \
            "No default victim noise config found for env_name:{}".format(env_name)
        fname = os.path.join(path_stem, default_configs[env_name])
        final_noise_params = json.load(open(fname))

        if noise_params is not None:
            config = json.load(open(noise_params))
            final_noise_params.update(config)
        return final_noise_params

    @staticmethod
    def _get_noise_generator(noise_type):
        noise_generators = {
            'gaussian': lambda x, size: np.random.normal(scale=x, size=size)
        }
        return noise_generators[noise_type]

    def predict(self, observation, state=None, mask=None, deterministic=False):
        original_actions, states = self.agent.predict(observation, state, mask, deterministic)
        action_shape = original_actions.shape
        noise_param = self.noise_annealer()

        noise = self.noise_generator(noise_param, action_shape)
        noisy_actions = original_actions * (1 + noise)
        return noisy_actions, states

    def reset(self):
        return self.agent.reset()

    def setup_model(self):
        pass

    def learn(self):
        raise NotImplementedError()

    def action_probability(self, observation, state=None, mask=None, actions=None):
        raise NotImplementedError()

    def save(self, save_path):
        raise NotImplementedError()

    def load(self):
        raise NotImplementedError()


def apply_env_wrapper(single_env, rew_shape_params, env_name, agent_idx, batch_size, scheduler):
    shaping_params = RewardShapingVecWrapper.get_shaping_params(rew_shape_params, env_name)
    rew_shape_anneal_frac = shaping_params.get('rew_shape_anneal_frac', 0)
    if rew_shape_anneal_frac > 0:
        rew_shape_func = LinearAnnealer(1, 0, rew_shape_anneal_frac).get_value
    else:
        # In this case, the different reward types are weighted differently
        # but reward is not annealed over time.
        rew_shape_func = None

    scheduler.set_func('rew_shape', rew_shape_func)
    return RewardShapingVecWrapper(single_env, agent_idx=agent_idx,
                                   shaping_params=shaping_params, batch_size=batch_size,
                                   reward_annealer=scheduler.get_func('rew_shape'))


def apply_victim_wrapper(victim, victim_noise_params, env_name, scheduler):
    noise_params = NoisyAgentWrapper.get_noise_params(victim_noise_params, env_name)
    victim_noise_anneal_frac = noise_params.get('victim_noise_anneal_frac', 0)
    victim_noise_param = noise_params.get('victim_noise_param', 0)
    assert victim_noise_anneal_frac > 0, \
        "victim_noise_anneal_frac must be greater than 0 if using a NoisyAgentWrapper."

    noise_anneal_func = LinearAnnealer(victim_noise_param, 0,
                                       victim_noise_anneal_frac).get_value
    scheduler.set_func('noise', noise_anneal_func)
    return NoisyAgentWrapper(victim, noise_annealer=scheduler.get_func('noise'))
