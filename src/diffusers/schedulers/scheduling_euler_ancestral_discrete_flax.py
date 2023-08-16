# Copyright 2023 Katherine Crowson and The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
import random
from typing import Optional, Tuple, Union

import flax
import jax
import jax.numpy as jnp

from ..configuration_utils import ConfigMixin, register_to_config
from .scheduling_utils_flax import (
    CommonSchedulerState,
    FlaxKarrasDiffusionSchedulers,
    FlaxSchedulerMixin,
    FlaxSchedulerOutput,
    broadcast_to_shape_from_left,
)


@flax.struct.dataclass
class EulerAncestralDiscreteSchedulerState:
    common: CommonSchedulerState

    # setable values
    init_noise_sigma: jnp.ndarray
    timesteps: jnp.ndarray
    sigmas: jnp.ndarray
    num_inference_steps: Optional[int] = None

    @classmethod
    def create(
        cls, common: CommonSchedulerState, init_noise_sigma: jnp.ndarray, timesteps: jnp.ndarray, sigmas: jnp.ndarray
    ):
        return cls(common=common, init_noise_sigma=init_noise_sigma, timesteps=timesteps, sigmas=sigmas)


@dataclass
class FlaxEulerAncestralDiscreteSchedulerOutput(FlaxSchedulerOutput):
    state: EulerAncestralDiscreteSchedulerState


class FlaxEulerAncestralDiscreteScheduler(FlaxSchedulerMixin, ConfigMixin):
    """
    Ancestral sampling with Euler method steps. Based on the original k-diffusion implementation by Katherine Crowson:
    https://github.com/crowsonkb/k-diffusion/blob/481677d114f6ea445aa009cf5bd7a9cdee909e47/k_diffusion/sampling.py#L72

    [`~ConfigMixin`] takes care of storing all config attributes that are passed in the scheduler's `__init__`
    function, such as `num_train_timesteps`. They can be accessed via `scheduler.config.num_train_timesteps`.
    [`SchedulerMixin`] provides general loading and saving functionality via the [`SchedulerMixin.save_pretrained`] and
    [`~SchedulerMixin.from_pretrained`] functions.

    Args:
        num_train_timesteps (`int`): number of diffusion steps used to train the model.
        beta_start (`float`): the starting `beta` value of inference.
        beta_end (`float`): the final `beta` value.
        beta_schedule (`str`):
            the beta schedule, a mapping from a beta range to a sequence of betas for stepping the model. Choose from
            `linear` or `scaled_linear`.
        trained_betas (`jnp.ndarray`, optional):
            option to pass an array of betas directly to the constructor to bypass `beta_start`, `beta_end` etc.
        prediction_type (`str`, default `epsilon`, optional):
            prediction type of the scheduler function, one of `epsilon` (predicting the noise of the diffusion
            process), `sample` (directly predicting the noisy sample`) or `v_prediction` (see section 2.4
            https://imagen.research.google/video/paper.pdf)
        dtype (`jnp.dtype`, *optional*, defaults to `jnp.float32`):
            the `dtype` used for params and computation.
    """

    _compatibles = [e.name for e in FlaxKarrasDiffusionSchedulers]

    dtype: jnp.dtype

    @property
    def has_state(self):
        return True

    @register_to_config
    def __init__(
        self,
        num_train_timesteps: int = 1000,
        beta_start: float = 0.0001,
        beta_end: float = 0.02,
        beta_schedule: str = "linear",
        trained_betas: Optional[jnp.ndarray] = None,
        prediction_type: str = "epsilon",
        dtype: jnp.dtype = jnp.float32,
    ):
        self.dtype = dtype

    def create_state(self, common: Optional[CommonSchedulerState] = None) -> EulerAncestralDiscreteSchedulerState:
        if common is None:
            common = CommonSchedulerState.create(self)

        timesteps = jnp.arange(0, self.config.num_train_timesteps).round()[::-1]
        sigmas = ((1 - common.alphas_cumprod) / common.alphas_cumprod) ** 0.5

        # standard deviation of the initial noise distribution
        init_noise_sigma = sigmas.max()

        return EulerAncestralDiscreteSchedulerState.create(
            common=common,
            init_noise_sigma=init_noise_sigma,
            timesteps=timesteps,
            sigmas=sigmas,
        )

    def scale_model_input(
        self, state: EulerAncestralDiscreteSchedulerState, sample: jnp.ndarray, timestep: int
    ) -> jnp.ndarray:
        """
        Scales the denoising model input by `(sigma**2 + 1) ** 0.5` to match the Euler algorithm.

        Args:
            state (`EulerAncestralDiscreteSchedulerState`):
                the `FlaxEulerAncestralDiscreteScheduler` state data class instance.
            sample (`jnp.ndarray`):
                current instance of sample being created by diffusion process.
            timestep (`int`):
                current discrete timestep in the diffusion chain.

        Returns:
            `jnp.ndarray`: scaled input sample
        """
        (step_index,) = jnp.where(state.timesteps == timestep, size=1)
        step_index = step_index[0]

        sigma = state.sigmas[step_index]
        sample = sample / ((sigma**2 + 1) ** 0.5)
        return sample

    def set_timesteps(
        self, state: EulerAncestralDiscreteSchedulerState, num_inference_steps: int, shape: Tuple = ()
    ) -> EulerAncestralDiscreteSchedulerState:
        """
        Sets the timesteps used for the diffusion chain. Supporting function to be run before inference.

        Args:
            state (`EulerAncestralDiscreteSchedulerState`):
                the `FlaxEulerAncestralDiscreteScheduler` state data class instance.
            num_inference_steps (`int`):
                the number of diffusion steps used when generating samples with a pre-trained model.
        """

        timesteps = jnp.linspace(self.config.num_train_timesteps - 1, 0, num_inference_steps, dtype=self.dtype)

        low_idx = jnp.floor(timesteps).astype(jnp.int32)
        high_idx = jnp.ceil(timesteps).astype(jnp.int32)

        frac = jnp.mod(timesteps, 1.0)

        sigmas = ((1 - state.common.alphas_cumprod) / state.common.alphas_cumprod) ** 0.5
        sigmas = (1 - frac) * sigmas[low_idx] + frac * sigmas[high_idx]
        sigmas = jnp.concatenate([sigmas, jnp.array([0.0], dtype=self.dtype)])

        # standard deviation of the initial noise distribution
        init_noise_sigma = sigmas.max()

        timesteps = timesteps.astype(jnp.int32)

        return state.replace(
            timesteps=timesteps,
            sigmas=sigmas,
            num_inference_steps=num_inference_steps,
            init_noise_sigma=init_noise_sigma,
        )

    def step(
        self,
        state: EulerAncestralDiscreteSchedulerState,
        model_output: jnp.ndarray,
        timestep: int,
        sample: jnp.ndarray,
        key: Optional[jax.random.KeyArray] = None,
        return_dict: bool = True,
    ) -> Union[FlaxEulerAncestralDiscreteSchedulerOutput, Tuple]:
        """
        Predict the sample at the previous timestep by reversing the SDE. Core function to propagate the diffusion
        process from the learned model outputs (most often the predicted noise).

        Args:
            state (`EulerAncestralDiscreteSchedulerState`):
                the `FlaxEulerAncestralDiscreteScheduler` state data class instance.
            model_output (`jnp.ndarray`): direct output from learned diffusion model.
            timestep (`int`): current discrete timestep in the diffusion chain.
            sample (`jnp.ndarray`):
                current instance of sample being created by diffusion process.
            order: coefficient for multi-step inference.
            return_dict (`bool`): option for returning tuple rather than FlaxEulerAncestralDiscreteScheduler class

        Returns:
            [`FlaxEulerAncestralDiscreteScheduler`] or `tuple`: [`FlaxEulerAncestralDiscreteScheduler`] if
            `return_dict` is True, otherwise a `tuple`. When returning a tuple, the first element is the sample tensor.

        """
        if state.num_inference_steps is None:
            raise ValueError(
                "Number of inference steps is 'None', you need to run 'set_timesteps' after creating the scheduler"
            )

        (step_index,) = jnp.where(state.timesteps == timestep, size=1)
        step_index = step_index[0]

        sigma = state.sigmas[step_index]

        # 1. compute predicted original sample (x_0) from sigma-scaled predicted noise
        if self.config.prediction_type == "epsilon":
            pred_original_sample = sample - sigma * model_output
        elif self.config.prediction_type == "v_prediction":
            # * c_out + input * c_skip
            pred_original_sample = model_output * (-sigma / (sigma**2 + 1) ** 0.5) + (sample / (sigma**2 + 1))
        else:
            raise ValueError(
                f"prediction_type given as {self.config.prediction_type} must be one of `epsilon`, or `v_prediction`"
            )

        # sigma_from = state.sigmas[step_index]
        # sigma_to = state.sigmas[step_index + 1]
        # sigma_up = jnp.sqrt((sigma_to**2 * (sigma_from**2 - sigma_to**2) / sigma_from**2))
        # sigma_down = jnp.sqrt((sigma_to**2 - sigma_up**2))

        # 2. Convert to an ODE derivative
        derivative = (sample - pred_original_sample) / sigma

        # dt = sigma_down - sigma
        dt = self.sigmas[step_index + 1] - sigma

        prev_sample = sample + derivative * dt

        # if key is None:
        #     key = jax.random.PRNGKey(random.randint(0, 2 ** 32 - 1))
        # else:
        #     key = jax.random.split(key, num=1)
        # noise = jax.random.normal(key, shape=model_output.shape, dtype=self.dtype)

        # prev_sample = prev_sample + noise * sigma_up

        if not return_dict:
            return (prev_sample, state)

        return FlaxEulerAncestralDiscreteSchedulerOutput(prev_sample=prev_sample, state=state)

    def add_noise(
        self,
        state: EulerAncestralDiscreteSchedulerState,
        original_samples: jnp.ndarray,
        noise: jnp.ndarray,
        timesteps: jnp.ndarray,
    ) -> jnp.ndarray:
        sigma = state.sigmas[timesteps].flatten()
        sigma = broadcast_to_shape_from_left(sigma, noise.shape)

        noisy_samples = original_samples + noise * sigma

        return noisy_samples

    def __len__(self):
        return self.config.num_train_timesteps
