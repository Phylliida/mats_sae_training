from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Optional, cast
from transformer_lens import HookedTransformer
import os
from collections import defaultdict

import torch

import wandb


@dataclass
class RunnerConfig(ABC):
    """
    The config that's shared across all runners.
    """

    # Data Generating Function (Model + Training Distibuion)
    model_name: str = "gelu-2l"
    model_class: type = HookedTransformer
    hook_point: str = "blocks.{layer}.hook_mlp_out"
    hook_point_eval: str = "blocks.0.attn.pattern"
    hook_point_layer: int = 0
    hook_point_head_index: Optional[int] = None
    dataset_path: str = "NeelNanda/c4-tokenized-2b"
    is_dataset_tokenized: bool = True
    context_size: int = 128
    use_cached_activations: bool = False
    cached_activations_path: Optional[str] = (
        None  # Defaults to "activations/{dataset}/{model}/{full_hook_name}_{hook_point_head_index}"
    )

    # SAE Parameters
    d_in: int = 512

    # Activation Store Parameters
    n_batches_in_buffer: int = 20
    total_training_tokens: int = 2_000_000
    store_batch_size: int = 32

    # Misc
    device: str | torch.device = "cpu"
    seed: int = 42
    dtype: torch.dtype = torch.float32
    resume: bool = False

    def __post_init__(self):
        # Autofill cached_activations_path unless the user overrode it
        if self.cached_activations_path is None:
            self.cached_activations_path = f"activations/{self.dataset_path.replace('/', '_')}/{self.model_name.replace('/', '_')}/{self.hook_point}"
            if self.hook_point_head_index is not None:
                self.cached_activations_path += f"_{self.hook_point_head_index}"

@dataclass
class LanguageModelSAERunnerConfig(RunnerConfig):
    """
    Configuration for training a sparse autoencoder on a language model.
    """

    # SAE Parameters
    b_dec_init_method: str = "geometric_median"
    expansion_factor: int = 4
    from_pretrained_path: Optional[str] = None
    d_sae: Optional[int] = None

    # Training Parameters
    l1_coefficient: float = 1e-3
    lp_norm: float = 1
    lr: float = 3e-4
    lr_end: float | None = None  # only used for cosine annealing, default is lr / 10
    lr_scheduler_name: str = (
        "constant"  # constant, cosineannealing, cosineannealingwarmrestarts
    )
    lr_warm_up_steps: int = 500
    lr_decay_steps: int = 0
    train_batch_size: int = 4096
    n_restart_cycles: int = 1  # only used for cosineannealingwarmrestarts

    # Resampling protocol args
    use_ghost_grads: bool = False  # want to change this to true on some timeline.
    feature_sampling_window: int = 2000
    dead_feature_window: int = 1000  # unless this window is larger feature sampling,

    dead_feature_threshold: float = 1e-8

    # WANDB
    log_to_wandb: bool = True
    wandb_project: str = "mats_sae_training_language_model"
    wandb_id: Optional[str] = None
    run_name: Optional[str] = None
    wandb_entity: Optional[str] = None
    wandb_log_frequency: int = 10

    # Misc
    checkpoint_every: int = 0
    checkpoint_path: str = "checkpoints"
    max_checkpoints: Optional[int] = None
    prepend_bos: bool = True
    verbose: bool = True
    model_kwargs: dict = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.expansion_factor, list):
            self.d_sae = self.d_in * self.expansion_factor
        self.tokens_per_buffer = (
            self.train_batch_size * self.context_size * self.n_batches_in_buffer
        )

        if self.run_name is None:
            self.run_name = f"{self.d_sae}-L1-{self.l1_coefficient}-LR-{self.lr}-Tokens-{self.total_training_tokens:3.3e}"

        if self.b_dec_init_method not in ["geometric_median", "mean", "zeros"]:
            raise ValueError(
                f"b_dec_init_method must be geometric_median, mean, or zeros. Got {self.b_dec_init_method}"
            )
        if self.b_dec_init_method == "zeros":
            print(
                "Warning: We are initializing b_dec to zeros. This is probably not what you want."
            )

        self.device = torch.device(self.device)

        if self.lr_end is None:
            self.lr_end = self.lr / 10

        if self.wandb_id is None:
            unique_id = cast(
                Any, wandb
            ).util.generate_id()  # not sure why this type is erroring
        else:
            unique_id = self.wandb_id
        self.checkpoint_path = f"{self.checkpoint_path}/{unique_id}"

        if self.verbose:
            print(
                f"Run name: {self.d_sae}-L1-{self.l1_coefficient}-LR-{self.lr}-Tokens-{self.total_training_tokens:3.3e}"
            )
            # Print out some useful info:
            n_tokens_per_buffer = (
                self.store_batch_size * self.context_size * self.n_batches_in_buffer
            )
            print(f"n_tokens_per_buffer (millions): {n_tokens_per_buffer / 10 **6}")
            n_contexts_per_buffer = self.store_batch_size * self.n_batches_in_buffer
            print(
                f"Lower bound: n_contexts_per_buffer (millions): {n_contexts_per_buffer / 10 **6}"
            )

            total_training_steps = self.total_training_tokens // self.train_batch_size
            print(f"Total training steps: {total_training_steps}")

            total_wandb_updates = total_training_steps // self.wandb_log_frequency
            print(f"Total wandb updates: {total_wandb_updates}")

            # how many times will we sample dead neurons?
            # assert self.dead_feature_window <= self.feature_sampling_window, "dead_feature_window must be smaller than feature_sampling_window"
            n_feature_window_samples = (
                total_training_steps // self.feature_sampling_window
            )
            print(
                f"n_tokens_per_feature_sampling_window (millions): {(self.feature_sampling_window * self.context_size * self.train_batch_size) / 10 **6}"
            )
            print(
                f"n_tokens_per_dead_feature_window (millions): {(self.dead_feature_window * self.context_size * self.train_batch_size) / 10 **6}"
            )
            print(
                f"We will reset the sparsity calculation {n_feature_window_samples} times."
            )
            # print("Number tokens in dead feature calculation window: ", self.dead_feature_window * self.train_batch_size)
            print(
                f"Number tokens in sparsity calculation window: {self.feature_sampling_window * self.train_batch_size:.2e}"
            )

        if self.use_ghost_grads:
            print("Using Ghost Grads.")

    def get_name(self):
        layers = self.hook_point_layer
        if not isinstance(layers, list):
            layers = [layers]
        if len(layers) > 1:
            layer_string = f"{min(layers)-max(layers)}"
        else:
            layer_string = f"{layers[0]}"
        sae_name = f"sae_group_{self.model_name.replace('/', '_')}_{self.hook_point.format(layer=layer_string)}_{self.d_sae}"
        return sae_name

    def get_base_path(self, checkpoint_name):
        return f"{self.checkpoint_path}/{checkpoint_name}_{self.get_name()}"
    
    def get_checkpoints_by_step(self):
        checkpoints = [f for f in os.listdir(self.checkpoint_path) if os.path.isfile(os.path.join(self.checkpoint_path, f))]
        mapped_to_steps = defaultdict(lambda: [])
        for c in checkpoints:
            pieces = c.split("_")
            steps = int(pieces[0])
            full_path = os.path.join(self.checkpoint_path, c)
            # there might be other saes here, ignore them
            if full_path.startswith(self.get_base_path(checkpoint_name=steps)):
                mapped_to_steps[steps].append(full_path)
        return mapped_to_steps

    def get_resume_base_path(self):
        mapped_to_steps = self.get_checkpoints_by_step()
        if len(mapped_to_steps) == 0:
            raise FileNotFoundError("no checkpoints available to resume from")
        else:
            max_step = max(list(mapped_to_steps.keys()))
            print(f"resuming from step {max_step}")
            return self.get_base_path(checkpoint_name=max_step)

@dataclass
class CacheActivationsRunnerConfig(RunnerConfig):
    """
    Configuration for caching activations of an LLM.
    """

    # Activation caching stuff
    shuffle_every_n_buffers: int = 10
    n_shuffles_with_last_section: int = 10
    n_shuffles_in_entire_dir: int = 10
    n_shuffles_final: int = 100
    model_kwargs: dict = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        if self.use_cached_activations:
            # this is a dummy property in this context; only here to avoid class compatibility headaches
            raise ValueError(
                "use_cached_activations should be False when running cache_activations_runner"
            )
