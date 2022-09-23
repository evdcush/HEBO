# Copyright (C) 2020. Huawei Technologies Co., Ltd. All rights reserved.

# This program is free software; you can redistribute it and/or modify it under
# the terms of the MIT license.

# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the MIT License for more details.
import warnings
from typing import Optional, List

import numpy as np
import pandas as pd
import torch

from comb_opt.optimizers.optimizer_base import OptimizerBase
from comb_opt.search_space import SearchSpace
from comb_opt.trust_region.tr_manager_base import TrManagerBase
from comb_opt.trust_region.tr_utils import sample_numeric_and_nominal_within_tr
from comb_opt.utils.dependant_rounding import DepRound
from comb_opt.utils.distance_metrics import hamming_distance


class MultiArmedBandit(OptimizerBase):

    @property
    def name(self) -> str:
        if self.tr_manager is not None:
            name = 'Tr-based Multi-Armed Bandit'
        else:
            name = 'Multi-Armed Bandit'
        return name

    def __init__(self,
                 search_space: SearchSpace,
                 batch_size: int = 1,
                 max_n_iter: int = 200,
                 noisy_black_box: bool = False,
                 resample_tol: int = 500,
                 fixed_tr_manager: Optional[TrManagerBase] = None,
                 fixed_tr_centre_nominal_dims: Optional[List] = None,
                 dtype: torch.dtype = torch.float32,
                 ):

        assert search_space.num_dims == search_space.num_nominal + search_space.num_ordinal, 'The Multi-armed bandit optimizer only supports nominal and ordinal variables.'

        self.batch_size = batch_size
        self.max_n_iter = max_n_iter
        self.resample_tol = resample_tol
        self.noisy_black_box = noisy_black_box

        self.n_cats = [int(ub + 1) for ub in search_space.nominal_ub + search_space.ordinal_ub]

        self.best_ube = 2 * max_n_iter / 3  # Upper bound estimate

        self.gamma = []
        for n_cats in self.n_cats:
            if n_cats > batch_size:
                self.gamma.append(np.sqrt(n_cats * np.log(n_cats / batch_size) / (
                        (np.e - 1) * batch_size * self.best_ube)))
            else:
                self.gamma.append(np.sqrt(n_cats * np.log(n_cats) / ((np.e - 1) * self.best_ube)))

        self.log_weights = [np.zeros(C) for C in self.n_cats]
        self.prob_dist = None

        if fixed_tr_manager is not None:
            assert 'nominal' in fixed_tr_manager.radii, 'Trust Region manager must contain a radius for nominal variables'
            assert fixed_tr_manager.center is not None, 'Trust Region does not have a centre. Call tr_manager.set_center(center) to set one.'
        self.tr_manager = fixed_tr_manager
        self.fixed_tr_centre_nominal_dims = fixed_tr_centre_nominal_dims
        self.tr_center = None if fixed_tr_manager is None else fixed_tr_manager.center.unsqueeze(0)[fixed_tr_centre_nominal_dims]

        super(MultiArmedBandit, self).__init__(search_space, dtype)

    def update_fixed_tr_manager(self, fixed_tr_manager: Optional[TrManagerBase], nominal_dims: Optional[List]):
        self.tr_manager = fixed_tr_manager
        self.tr_center = None if fixed_tr_manager is None else fixed_tr_manager.center[self.fixed_tr_centre_nominal_dims].unsqueeze(0)

    def method_suggest(self, n_suggestions: int = 1) -> pd.DataFrame:

        self.update_prob_dist()

        x_next = torch.zeros((n_suggestions, self.search_space.num_dims), dtype=self.dtype)

        # Sample all the categorical variables
        for j, num_cat in enumerate(self.n_cats):
            # draw a batch here
            if 1 < n_suggestions < num_cat:
                ht = DepRound(self.prob_dist[j], k=n_suggestions)
            else:
                ht = np.random.choice(num_cat, n_suggestions, p=self.prob_dist[j])

            # ht_batch_list size: len(self.C_list) x B
            x_next[:, j] = torch.tensor(ht[:], dtype=self.dtype)

        # Project back all point to the trust region centre
        if self.tr_manager is not None:
            hamming_distances = hamming_distance(x_next, self.tr_center, normalize=False)

            for sample_idx, distance in enumerate(hamming_distances):
                if distance > self.tr_manager.get_nominal_radius():
                    # Project x back to the trust region
                    mask = x_next[sample_idx] != self.tr_center[0]
                    indices = np.random.choice([i for i, x in enumerate(mask) if x],
                                               size=distance.item() - self.tr_manager.get_nominal_radius(),
                                               replace=False)
                    x_next[sample_idx][indices] = self.tr_center[0][indices]

        # Eliminate suggestions that have already been observed and all duplicates in the current batch
        for sample_idx in range(n_suggestions):
            tol = 0
            seen = self.was_sample_seen(x_next, sample_idx)

            while seen:
                # Resample
                for j, num_cat in enumerate(self.n_cats):
                    ht = np.random.choice(num_cat, 1, p=self.prob_dist[j])
                    x_next[sample_idx, j] = torch.tensor(ht[:], dtype=self.dtype)

                # Project back all point to the trust region centre
                if self.tr_manager is not None:
                    dist = hamming_distance(x_next[sample_idx:sample_idx + 1], self.tr_center, normalize=False)
                    if dist.item() > self.tr_manager.get_nominal_radius():
                        # Project x back to the trust region
                        mask = x_next[sample_idx] != self.tr_center[0]
                        indices = np.random.choice([i for i, x in enumerate(mask) if x],
                                                   size=dist.item() - self.tr_manager.get_nominal_radius(),
                                                   replace=False)
                        x_next[sample_idx][indices] = self.tr_center[0][indices]

                seen = self.was_sample_seen(x_next, sample_idx)
                tol += 1

                if tol > self.resample_tol:
                    warnings.warn(
                        f'Failed to sample a previously unseen sample within {self.resample_tol} attempts. Consider increasing the \'resample_tol\' parameter. Generating a random sample...')
                    if self.tr_manager is not None:
                        x_next[sample_idx] = sample_numeric_and_nominal_within_tr(x_centre=self.tr_center,
                                                                                  search_space=self.search_space,
                                                                                  tr_manager=self.tr_manager,
                                                                                  n_points=1,
                                                                                  numeric_dims=[],
                                                                                  discrete_choices=[],
                                                                                  max_n_perturb_num=0,
                                                                                  model=None,
                                                                                  return_numeric_bounds=False)[0]
                    else:
                        x_next[sample_idx] = self.search_space.transform(self.search_space.sample(1))[0]

                    seen = False  # Needed to prevent infinite loop

        return self.search_space.inverse_transform(x_next)

    def was_sample_seen(self, x_next, sample_idx):

        seen = False

        if len(x_next) > 1:
            # Check if current sample is already in the batch
            if (x_next[sample_idx:sample_idx + 1] == torch.cat((x_next[:sample_idx], x_next[sample_idx + 1:]))).all(
                    dim=1).any():
                seen = True

        # If the black-box is not noisy, check if the current sample was previously observed
        if (not seen) and (not self.noisy_black_box) and (x_next[sample_idx:sample_idx + 1] == self.data_buffer.x).all(
                dim=1).any():
            seen = True

        return seen

    def observe(self, x: pd.DataFrame, y: np.ndarray):

        # Transform x and y to torch tensors
        x = self.search_space.transform(x)

        if isinstance(y, np.ndarray):
            y = torch.tensor(y, dtype=self.dtype)

        assert len(x) == len(y)

        # Add data to all previously observed data and to the trust region manager
        self.data_buffer.append(x, y)

        # update best x and y
        if self.best_y is None:
            batch_idx = y.flatten().argmin()
            self.best_y = y[batch_idx, 0].item()
            self._best_x = x[batch_idx: batch_idx + 1]

        else:
            batch_idx = y.flatten().argmin()
            y_ = y[batch_idx, 0].item()

            if y_ < self.best_y:
                self.best_y = y_
                self._best_x = x[batch_idx: batch_idx + 1]

        # Compute the MAB rewards for each of the suggested categories
        mab_rewards = torch.zeros((len(x), self.search_space.num_dims), dtype=self.dtype)

        # Iterate over the batch
        for batch_idx in range(len(x)):
            _x = x[batch_idx]

            # Iterate over all categorical variables
            for dim_dix in range(self.search_space.num_dims):
                indices = self.data_buffer.x[:, dim_dix] == _x[dim_dix]

                # In MAB, we aim to maximise the reward. Comb Opt optimizers minimize reward, hence, take negative of bb values
                rewards = - self.data_buffer.y[indices]

                if len(rewards) == 0:
                    reward = torch.tensor(0., dtype=self.dtype)
                else:
                    reward = rewards.max()

                    # If possible, map rewards to range[-0.5, 0.5]
                    if self.data_buffer.y.max() != self.data_buffer.y.min():
                        reward = 2 * (rewards.max() - (- self.data_buffer.y).min()) / \
                                 ((- self.data_buffer.y).max() - (-self.data_buffer.y).min()) - 1.

                mab_rewards[batch_idx, dim_dix] = reward

        # Update the probability distribution
        for dim_dix in range(self.search_space.num_dims):
            log_weights = self.log_weights[dim_dix]
            num_cats = self.n_cats[dim_dix]
            gamma = self.gamma[dim_dix]
            prob_dist = self.prob_dist[dim_dix]

            x = x.to(torch.long)
            reward = mab_rewards[:, dim_dix]
            nominal_vars = x[:, dim_dix]  # 1xB
            for ii, ht in enumerate(nominal_vars):
                Gt_ht_b = reward[ii]
                estimated_reward = 1.0 * Gt_ht_b / prob_dist[ht]
                # if ht not in self.S0:
                log_weights[ht] = (log_weights[ht] + (len(mab_rewards) * estimated_reward * gamma / num_cats)).clip(
                    min=-30, max=30)

            self.log_weights[dim_dix] = log_weights

    def restart(self):
        self._restart()

        self.gamma = []
        for n_cats in self.n_cats:
            if n_cats > self.batch_size:
                self.gamma.append(np.sqrt(n_cats * np.log(n_cats / self.batch_size) / (
                        (np.e - 1) * self.batch_size * self.best_ube)))
            else:
                self.gamma.append(np.sqrt(n_cats * np.log(n_cats) / ((np.e - 1) * self.best_ube)))

        self.log_weights = [np.zeros(C) for C in self.n_cats]
        self.prob_dist = None

    def set_x_init(self, x: pd.DataFrame):
        # This does not apply to the MAB algorithm
        warnings.warn('set_x_init does not apply to the MAB algorithm')
        pass

    def initialize(self, x: pd.DataFrame, y: np.ndarray):

        # Transform x and y to torch tensors
        x = self.search_space.transform(x)

        if isinstance(y, np.ndarray):
            y = torch.tensor(y, dtype=self.dtype)

        assert len(x) == len(y)

        # Add data to all previously observed data and to the trust region manager
        self.data_buffer.append(x, y)

        # update best x and y
        if self.best_y is None:
            batch_idx = y.flatten().argmin()
            self.best_y = y[batch_idx, 0].item()
            self._best_x = x[batch_idx: batch_idx + 1]

        else:
            batch_idx = y.flatten().argmin()
            y_ = y[batch_idx, 0].item()

            if y_ < self.best_y:
                self.best_y = y_
                self._best_x = x[batch_idx: batch_idx + 1]

    def update_prob_dist(self):

        prob_dist = []

        for j in range(len(self.n_cats)):
            weights = np.exp(self.log_weights[j])
            gamma = self.gamma[j]
            norm = float(sum(weights))
            prob_dist.append(list((1.0 - gamma) * (w / norm) + (gamma / len(weights)) for w in weights))

        self.prob_dist = prob_dist
