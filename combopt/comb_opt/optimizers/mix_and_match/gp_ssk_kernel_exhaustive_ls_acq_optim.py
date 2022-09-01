# Copyright (C) 2020. Huawei Technologies Co., Ltd. All rights reserved.

# This program is free software; you can redistribute it and/or modify it under
# the terms of the MIT license.

# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the MIT License for more details.

from typing import Optional

import torch
from gpytorch.constraints import Interval
from gpytorch.kernels import ScaleKernel
from gpytorch.priors import Prior

from comb_opt.acq_funcs import acq_factory
from comb_opt.acq_optimizers.exhaustive_local_search_acq_optimizer import ExhaustiveLsAcqOptimizer
from comb_opt.models.gp import ExactGPModel
from comb_opt.models.gp.kernels import SubStringKernel
from comb_opt.optimizers import BoBase
from comb_opt.search_space import SearchSpace
from comb_opt.utils.graph_utils import laplacian_eigen_decomposition


class GpSskExhaustiveLsAcqOptim(BoBase):

    @property
    def name(self) -> str:
        return 'GP (SSK) - LS acq optim'

    def __init__(self,
                 search_space: SearchSpace,
                 n_init: int,
                 model_max_subsequence_length: int = 3,
                 model_kernel_gap_decay: float = 0.5,
                 model_kernel_match_decay: float = 0.8,
                 model_normalize_kernel_output: bool = True,
                 model_noise_prior: Optional[Prior] = None,
                 model_noise_constr: Optional[Interval] = None,
                 model_noise_lb: float = 1e-5,
                 model_pred_likelihood: bool = True,
                 model_optimiser: str = 'adam',
                 model_lr: float = 3e-2,
                 model_num_epochs: int = 100,
                 model_max_cholesky_size: int = 2000,
                 model_max_training_dataset_size: int = 1000,
                 model_max_batch_size: int = 5000,
                 acq_name: str = 'ei',
                 acq_optim_n_random_vertices: int = 20000,
                 acq_optim_n_greedy_ascent_init: int = 20,
                 acq_optim_n_spray: int = 10,
                 acq_optim_max_n_ascent: float = float('inf'),
                 dtype: torch.dtype = torch.float32,
                 device: torch.device = torch.device('cpu')
                 ):
        assert search_space.num_nominal + search_space.num_ordinal == search_space.num_params, \
            'This Optimiser only supports nominal and ordinal variables.'

        # Eigen decomposition of the graph laplacian
        n_vertices, adjacency_mat_list, fourier_freq_list, fourier_basis_list = laplacian_eigen_decomposition(
            search_space)

        alphabet = search_space.params[search_space.param_names[0]].categories
        for param in search_space.params:
            assert search_space.params[param].categories == alphabet, \
                '\'categories\' must be the same for each of the nominal variables.'

        # Initialise the model
        alphabet_size = len(alphabet)

        # Initialise the model
        kernel = ScaleKernel(SubStringKernel(seq_length=search_space.num_dims,
                                             alphabet_size=alphabet_size,
                                             gap_decay=model_kernel_gap_decay,
                                             match_decay=model_kernel_match_decay,
                                             max_subsequence_length=model_max_subsequence_length,
                                             normalize=model_normalize_kernel_output,
                                             device=device))

        # Initialise the model
        model = ExactGPModel(search_space=search_space,
                             num_out=1,
                             kernel=kernel,
                             noise_prior=model_noise_prior,
                             noise_constr=model_noise_constr,
                             noise_lb=model_noise_lb,
                             pred_likelihood=model_pred_likelihood,
                             lr=model_lr,
                             num_epochs=model_num_epochs,
                             optimizer=model_optimiser,
                             max_cholesky_size=model_max_cholesky_size,
                             max_training_dataset_size=model_max_training_dataset_size,
                             max_batch_size=model_max_batch_size,
                             dtype=dtype,
                             device=device)

        # Initialise the acquisition function
        acq_func = acq_factory(acq_func_name=acq_name)

        acq_optim = ExhaustiveLsAcqOptimizer(search_space=search_space,
                                             adjacency_mat_list=adjacency_mat_list,
                                             n_vertices=n_vertices,
                                             n_random_vertices=acq_optim_n_random_vertices,
                                             n_greedy_ascent_init=acq_optim_n_greedy_ascent_init,
                                             n_spray=acq_optim_n_spray,
                                             max_n_ascent=acq_optim_max_n_ascent,
                                             dtype=dtype)

        super(GpSskExhaustiveLsAcqOptim, self).__init__(search_space, n_init, model, acq_func, acq_optim, None, dtype,
                                                        device)