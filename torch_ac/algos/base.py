from abc import ABC, abstractmethod
import torch

from torch_ac.format import default_preprocess_obss
from torch_ac.utils import DictList, ParallelEnv
import collections


class BaseAlgo(ABC):
    """The base class for RL algorithms."""

    def __init__(self, envs, acmodel, device, num_frames_per_proc, discount, lr, gae_lambda, entropy_coef,
                 value_loss_coef, max_grad_norm, recurrence, preprocess_obss, reshape_reward):
        """
        Initializes a `BaseAlgo` instance.

        Parameters:
        ----------
        envs : list
            a list of environments that will be run in parallel
        acmodel : torch.Module
            the model
        num_frames_per_proc : int
            the number of frames collected by every process for an update
        discount : float
            the discount for future rewards
        lr : float
            the learning rate for optimizers
        gae_lambda : float
            the lambda coefficient in the GAE formula
            ([Schulman et al., 2015](https://arxiv.org/abs/1506.02438))
        entropy_coef : float
            the weight of the entropy cost in the final objective
        value_loss_coef : float
            the weight of the value loss in the final objective
        max_grad_norm : float
            gradient will be clipped to be at most this value
        recurrence : int
            the number of steps the gradient is propagated back in time
        preprocess_obss : function
            a function that takes observations returned by the environment
            and converts them into the format that the model can handle
        reshape_reward : function
            a function that shapes the reward, takes an
            (observation, action, reward, done) tuple as an input
        """

        # Store parameters

        self.env = ParallelEnv(envs)
        self.acmodel = acmodel
        self.device = device
        self.num_frames_per_proc = num_frames_per_proc
        self.discount = discount
        self.lr = lr
        self.gae_lambda = gae_lambda
        self.entropy_coef = entropy_coef
        self.value_loss_coef = value_loss_coef
        self.max_grad_norm = max_grad_norm
        self.recurrence = recurrence
        self.preprocess_obss = preprocess_obss or default_preprocess_obss
        self.reshape_reward = reshape_reward

        # Control parameters

        assert self.acmodel.recurrent or self.recurrence == 1
        assert self.num_frames_per_proc % self.recurrence == 0

        # Configure acmodel

        self.acmodel.to(self.device)
        self.acmodel.train()

        # Store helpers values

        self.num_procs = len(envs)
        self.num_frames = self.num_frames_per_proc * self.num_procs

        # Initialize experience values

        shape = (self.num_frames_per_proc, self.num_procs)

        self.obs = self.env.reset()
        self.obss = [None] * (shape[0])
        if self.acmodel.recurrent:
            self.memory = torch.zeros(shape[1], self.acmodel.memory_size, device=self.device)
            self.memories = torch.zeros(*shape, self.acmodel.memory_size, device=self.device)
        self.mask = torch.ones(shape[1], device=self.device)
        self.masks = torch.zeros(*shape, device=self.device)
        self.actions = torch.zeros(*shape, device=self.device, dtype=torch.int)
        self.values = torch.zeros(*shape, device=self.device)
        self.rewards = torch.zeros(*shape, device=self.device)
        self.rewards_PERFORMANCE = torch.zeros(*shape, device=self.device)
        self.rewards_BUTTON_PRESSES = torch.zeros(*shape, device=self.device)
        self.rewards_PHONES_CLEANED = torch.zeros(*shape, device=self.device)
        self.rewards_DIRT_CLEANED = torch.zeros(*shape, device=self.device)
        self.advantages = torch.zeros(*shape, device=self.device)
        self.log_probs = torch.zeros(*shape, device=self.device)

        # Initialize log values

        self.log_episode_return_MESSES = torch.zeros(self.num_procs, device=self.device)

        self.log_episode_return_PERFORMANCE_FULL = torch.zeros(self.num_procs, device=self.device)

        self.log_episode_return_PERFORMANCE = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_return_BUTTON_PRESSES = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_return_PHONES_CLEANED = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_return_DIRT_CLEANED = torch.zeros(self.num_procs, device=self.device)

        self.log_episode_return = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_reshaped_return = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_reshaped_return_PERFORMANCE = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_reshaped_return_BUTTON_PRESSES = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_reshaped_return_PHONES_CLEANED = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_reshaped_return_DIRT_CLEANED = torch.zeros(self.num_procs, device=self.device)
        self.log_episode_num_frames = torch.zeros(self.num_procs, device=self.device)

        self.log_done_counter = 0
        self.log_return = [0] * self.num_procs
        self.log_return_MESSES = [0] * self.num_procs
        self.log_return_PERFORMANCE_FULL = [0] * self.num_procs
        self.log_return_PERFORMANCE = [0] * self.num_procs
        self.log_return_BUTTON_PRESSES = [0] * self.num_procs
        self.log_return_PHONES_CLEANED = [0] * self.num_procs
        self.log_return_DIRT_CLEANED = [0] * self.num_procs
        self.log_reshaped_return = [0] * self.num_procs
        self.log_reshaped_return_PERFORMANCE = [0] * self.num_procs
        self.log_reshaped_return_BUTTON_PRESSES = [0] * self.num_procs
        self.log_reshaped_return_PHONES_CLEANED = [0] * self.num_procs
        self.log_reshaped_return_DIRT_CLEANED = [0] * self.num_procs
        self.log_num_frames = [0] * self.num_procs

    def collect_experiences(self):
        """Collects rollouts and computes advantages.

        Runs several environments concurrently. The next actions are computed
        in a batch mode for all environments at the same time. The rollouts
        and advantages from all environments are concatenated together.

        Returns
        -------
        exps : DictList
            Contains actions, rewards, advantages etc as attributes.
            Each attribute, e.g. `exps.reward` has a shape
            (self.num_frames_per_proc * num_envs, ...). k-th block
            of consecutive `self.num_frames_per_proc` frames contains
            data obtained from the k-th environment. Be careful not to mix
            data from different environments!
        logs : dict
            Useful stats about the training process, including the average
            reward, policy loss, value loss, etc.
        """

        hasMesses = False
        hasPerf = False
        hasPerfFull = False
        hasButtonPresses = False
        hasPhonesCleaned = False
        hasDirtCleaned = False

        addedAllMyData = False
        loggedAllMyData = False
        allMyData = None

        for i in range(self.num_frames_per_proc):
            # Do one agent-environment interaction

            preprocessed_obs = self.preprocess_obss(self.obs, device=self.device)
            with torch.no_grad():
                if self.acmodel.recurrent:
                    dist, value, memory = self.acmodel(preprocessed_obs, self.memory * self.mask.unsqueeze(1))
                else:
                    dist, value = self.acmodel(preprocessed_obs)
            action = dist.sample()

            obs, reward, done, info = self.env.step(action.cpu().numpy())

            if not addedAllMyData:
                pass
                # for iiiii in info:
                #     allMyData_ = iiiii.get('allMyData', None)
                #     if allMyData_:
                #         addedAllMyData = True
                #         allMyData = allMyData_

            if 'messes_cleaned' in info[0]:
                hasMesses = True
                messes = tuple([i['messes_cleaned'] for i in info])

            if 'performance_full' in info[0]:
                hasPerfFull = True
                performancesFULL = tuple([i['performance_full'] for i in info])

            if 'performance' in info[0]:
                hasPerf = True
                performances = tuple([i['performance'] for i in info])

            if 'button_presses' in info[0]:
                hasButtonPresses = True
                button_presses = tuple([i['button_presses'] for i in info])

            if 'phones_cleaned' in info[0]:
                hasPhonesCleaned = True
                phones_cleaned = tuple([i['phones_cleaned'] for i in info])

            if 'dirt_cleaned' in info[0]:
                hasDirtCleaned = True
                dirt_cleaned = tuple([i['dirt_cleaned'] for i in info])

            # Update experiences values

            self.obss[i] = self.obs
            self.obs = obs
            if self.acmodel.recurrent:
                self.memories[i] = self.memory
                self.memory = memory
            self.masks[i] = self.mask
            self.mask = 1 - torch.tensor(done, device=self.device, dtype=torch.float)
            self.actions[i] = action
            self.values[i] = value
            if self.reshape_reward is not None:
                self.rewards[i] = torch.tensor([
                    self.reshape_reward(obs_, action_, reward_, done_)
                    for obs_, action_, reward_, done_ in zip(obs, action, reward, done)
                ], device=self.device)

                assert False, "no idea how to use performance here"
            else:
                self.rewards[i] = torch.tensor(reward, device=self.device)
                if hasPerf:
                    self.rewards_PERFORMANCE[i] = torch.tensor(performances, device=self.device)
                if hasButtonPresses:
                    self.rewards_BUTTON_PRESSES[i] = torch.tensor(button_presses, device=self.device)
                if hasPhonesCleaned:
                    self.rewards_PHONES_CLEANED[i] = torch.tensor(phones_cleaned, device=self.device)
                if hasDirtCleaned:
                    self.rewards_DIRT_CLEANED[i] = torch.tensor(dirt_cleaned, device=self.device)

            self.log_probs[i] = dist.log_prob(action)

            # Update log values
            if hasMesses:
                self.log_episode_return_MESSES += torch.tensor(messes, device=self.device, dtype=torch.float)
            # Update log values
            if hasPerfFull:
                self.log_episode_return_PERFORMANCE_FULL += torch.tensor(performancesFULL, device=self.device,
                                                                         dtype=torch.float)
            # Update log values
            if hasPerf:
                self.log_episode_return_PERFORMANCE += torch.tensor(performances, device=self.device, dtype=torch.float)
            # Update log values
            if hasButtonPresses:
                self.log_episode_return_BUTTON_PRESSES += torch.tensor(button_presses, device=self.device,
                                                                       dtype=torch.float)
            # Update log values
            if hasPhonesCleaned:
                self.log_episode_return_PHONES_CLEANED += torch.tensor(phones_cleaned, device=self.device,
                                                                       dtype=torch.float)
            # Update log values
            if hasDirtCleaned:
                self.log_episode_return_DIRT_CLEANED += torch.tensor(dirt_cleaned, device=self.device,
                                                                     dtype=torch.float)

            self.log_episode_return += torch.tensor(reward, device=self.device, dtype=torch.float)
            self.log_episode_reshaped_return += self.rewards[i]

            self.log_episode_reshaped_return_PERFORMANCE += self.rewards_PERFORMANCE[i]
            self.log_episode_reshaped_return_BUTTON_PRESSES += self.rewards_BUTTON_PRESSES[i]
            self.log_episode_reshaped_return_PHONES_CLEANED += self.rewards_PHONES_CLEANED[i]
            self.log_episode_reshaped_return_DIRT_CLEANED += self.rewards_DIRT_CLEANED[i]

            self.log_episode_num_frames += torch.ones(self.num_procs, device=self.device)

            for i, done_ in enumerate(done):
                if done_:
                    self.log_done_counter += 1

                    if hasMesses:
                        self.log_return_MESSES.append(self.log_episode_return_MESSES[i].item())

                    if hasPerfFull:
                        self.log_return_PERFORMANCE_FULL.append(self.log_episode_return_PERFORMANCE_FULL[i].item())

                    if hasPerf:
                        self.log_return_PERFORMANCE.append(self.log_episode_return_PERFORMANCE[i].item())
                        self.log_reshaped_return_PERFORMANCE.append(
                            self.log_episode_reshaped_return_PERFORMANCE[i].item())

                    if hasPhonesCleaned:
                        self.log_return_BUTTON_PRESSES.append(self.log_episode_return_BUTTON_PRESSES[i].item())
                        self.log_reshaped_return_BUTTON_PRESSES.append(
                            self.log_episode_reshaped_return_BUTTON_PRESSES[i].item())

                    if hasButtonPresses:
                        self.log_return_PHONES_CLEANED.append(self.log_episode_return_PHONES_CLEANED[i].item())
                        self.log_reshaped_return_PHONES_CLEANED.append(
                            self.log_episode_reshaped_return_PHONES_CLEANED[i].item())

                    if hasDirtCleaned:
                        self.log_return_DIRT_CLEANED.append(self.log_episode_return_DIRT_CLEANED[i].item())
                        self.log_reshaped_return_DIRT_CLEANED.append(
                            self.log_episode_reshaped_return_DIRT_CLEANED[i].item())

                    self.log_return.append(self.log_episode_return[i].item())
                    self.log_reshaped_return.append(self.log_episode_reshaped_return[i].item())

                    self.log_num_frames.append(self.log_episode_num_frames[i].item())

            self.log_episode_return *= self.mask
            self.log_episode_return_PERFORMANCE_FULL *= self.mask
            self.log_episode_return_MESSES *= self.mask
            self.log_episode_return_PERFORMANCE *= self.mask
            self.log_episode_return_BUTTON_PRESSES *= self.mask
            self.log_episode_return_PHONES_CLEANED *= self.mask
            self.log_episode_return_DIRT_CLEANED *= self.mask

            self.log_episode_reshaped_return *= self.mask
            self.log_episode_reshaped_return_PERFORMANCE *= self.mask
            self.log_episode_reshaped_return_BUTTON_PRESSES *= self.mask
            self.log_episode_reshaped_return_PHONES_CLEANED *= self.mask
            self.log_episode_reshaped_return_DIRT_CLEANED *= self.mask
            self.log_episode_num_frames *= self.mask

        # Add advantage and return to experiences

        preprocessed_obs = self.preprocess_obss(self.obs, device=self.device)
        with torch.no_grad():
            if self.acmodel.recurrent:
                _, next_value, _ = self.acmodel(preprocessed_obs, self.memory * self.mask.unsqueeze(1))
            else:
                _, next_value = self.acmodel(preprocessed_obs)

        for i in reversed(range(self.num_frames_per_proc)):
            next_mask = self.masks[i + 1] if i < self.num_frames_per_proc - 1 else self.mask
            next_value = self.values[i + 1] if i < self.num_frames_per_proc - 1 else next_value
            next_advantage = self.advantages[i + 1] if i < self.num_frames_per_proc - 1 else 0

            delta = self.rewards[i] + self.discount * next_value * next_mask - self.values[i]
            self.advantages[i] = delta + self.discount * self.gae_lambda * next_advantage * next_mask

        # Define experiences:
        #   the whole experience is the concatenation of the experience
        #   of each process.
        # In comments below:
        #   - T is self.num_frames_per_proc,
        #   - P is self.num_procs,
        #   - D is the dimensionality.

        exps = DictList()
        exps.obs = [self.obss[i][j]
                    for j in range(self.num_procs)
                    for i in range(self.num_frames_per_proc)]
        if self.acmodel.recurrent:
            # T x P x D -> P x T x D -> (P * T) x D
            exps.memory = self.memories.transpose(0, 1).reshape(-1, *self.memories.shape[2:])
            # T x P -> P x T -> (P * T) x 1
            exps.mask = self.masks.transpose(0, 1).reshape(-1).unsqueeze(1)
        # for all tensors below, T x P -> P x T -> P * T
        exps.action = self.actions.transpose(0, 1).reshape(-1)
        exps.value = self.values.transpose(0, 1).reshape(-1)
        exps.reward = self.rewards.transpose(0, 1).reshape(-1)
        exps.advantage = self.advantages.transpose(0, 1).reshape(-1)
        exps.returnn = exps.value + exps.advantage
        exps.log_prob = self.log_probs.transpose(0, 1).reshape(-1)

        # Preprocess experiences

        exps.obs = self.preprocess_obss(exps.obs, device=self.device)

        # Log some values

        keep = max(self.log_done_counter, self.num_procs)

        logs = {
            "return_per_episode": self.log_return[-keep:],
            "reshaped_return_per_episode": self.log_reshaped_return[-keep:],
            "num_frames_per_episode": self.log_num_frames[-keep:],
            "num_frames": self.num_frames,

            "messes_per_episode": self.log_return_MESSES[-keep:],
            "performance_full_per_episode": self.log_return_PERFORMANCE_FULL[-keep:],

            "performance_per_episode": self.log_return_PERFORMANCE[-keep:],
            "reshaped_performance_per_episode": self.log_reshaped_return_PERFORMANCE[-keep:],

            "buttons_per_episode": self.log_return_BUTTON_PRESSES[-keep:],
            "reshaped_buttons_per_episode": self.log_reshaped_return_BUTTON_PRESSES[-keep:],

            "phones_per_episode": self.log_return_PHONES_CLEANED[-keep:],
            "reshaped_phones_per_episode": self.log_reshaped_return_PHONES_CLEANED[-keep:],

            "dirt_per_episode": self.log_return_DIRT_CLEANED[-keep:],
            "reshaped_dirt_per_episode": self.log_reshaped_return_DIRT_CLEANED[-keep:],
            "numberOfPermutes": info[0]['numberOfPermutes'],
            "buttonValue": info[0]['buttonValue'],
        }

        if allMyData and not loggedAllMyData:
            # loggedAllMyData = True
            # logs['allMyData'] = allMyData
            pass

        self.log_done_counter = 0
        self.log_return = self.log_return[-self.num_procs:]
        self.log_reshaped_return = self.log_reshaped_return[-self.num_procs:]
        self.log_num_frames = self.log_num_frames[-self.num_procs:]

        return exps, logs

    @abstractmethod
    def update_parameters(self):
        pass
