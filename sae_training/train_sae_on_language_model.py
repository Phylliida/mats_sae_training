from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast, Optional

import torch
from torch.optim import Adam, Optimizer
from torch.optim.lr_scheduler import LRScheduler
import numpy as np
import random
from tqdm import tqdm
from transformer_lens import HookedTransformer
from transformer_lens.hook_points import HookedRootModule
import signal
import pickle
import os
import copy
import wandb
from sae_training.activations_store import ActivationsStore, HfDataset
from sae_training.evals import run_evals
from sae_training.geometric_median import compute_geometric_median
from sae_training.optim import get_scheduler
from sae_training.sae_group import SAEGroup
from sae_training.sparse_autoencoder import SparseAutoencoder


@dataclass
class SAETrainContext:
    """
    Context to track during training for a single SAE
    """

    act_freq_scores: torch.Tensor
    n_forward_passes_since_fired: torch.Tensor
    n_frac_active_tokens: int
    optimizer: Optimizer
    scheduler: LRScheduler

    @property
    def feature_sparsity(self) -> torch.Tensor:
        return self.act_freq_scores / self.n_frac_active_tokens


@dataclass
class SAETrainingRunState:
    """
    Training run state for all SAES
    Also includes n_training_steps
    n_training_tokens
    and rng states
    """

    train_contexts: list[SAETrainContext]
    n_training_steps : int = 0
    n_training_tokens : int = 0
    torch_state: Optional[torch.ByteTensor] = None
    torch_cuda_state: Optional[torch.ByteTensor] = None
    numpy_state: Optional[np.ndarray] = None
    random_state: Optional[Any] = None

    def __post_init__(self):
        if self.torch_state is None:
            self.torch_state = torch.get_rng_state()
        if self.torch_cuda_state is None:
            self.torch_cuda_state = torch.cuda.get_rng_state_all() 
        if self.numpy_state is None:
            self.numpy_state = np.random.get_state()
        if self.random_state is None:
            self.random_state = random.getstate()

    def set_random_state(self):
        torch.random.set_rng_state(self.torch_state)
        torch.cuda.set_rng_state_all(self.torch_cuda_state)
        np.random.set_state(self.numpy_state)
        random.setstate(self.random_state)

@dataclass
class TrainSAEGroupOutput:
    sae_group: SAEGroup
    checkpoint_paths: list[str]
    log_feature_sparsities: list[torch.Tensor]


def train_sae_on_language_model(
    model: HookedTransformer,
    sae_group: SAEGroup,
    activation_store: ActivationsStore,
    batch_size: int = 1024,
    feature_sampling_window: int = 1000,  # how many training steps between resampling the features / considiring neurons dead
    dead_feature_threshold: float = 1e-8,  # how infrequently a feature has to be active to be considered dead
    use_wandb: bool = False,
    wandb_log_frequency: int = 50,
) -> SAEGroup:
    """
    @deprecated Use `train_sae_group_on_language_model` instead. This method is kept for backward compatibility.
    """
    return train_sae_group_on_language_model(
        model,
        sae_group,
        activation_store,
        batch_size,
        feature_sampling_window,
        use_wandb,
        wandb_log_frequency,
    ).sae_group


def train_sae_group_on_language_model(
    model: HookedTransformer,
    sae_group: SAEGroup,
    activation_store: ActivationsStore,
    training_run_state: Optional[SAETrainingRunState] = None,
    batch_size: int = 1024,
    feature_sampling_window: int = 1000,  # how many training steps between resampling the features / considiring neurons dead
    use_wandb: bool = False,
    wandb_log_frequency: int = 50,
) -> TrainSAEGroupOutput:
    total_training_tokens = sae_group.cfg.total_training_tokens
    total_training_steps = total_training_tokens // batch_size
    n_training_steps = 0
    n_training_tokens = 0

    all_layers = sae_group.cfg.hook_point_layer
    if not isinstance(all_layers, list):
        all_layers = [all_layers]

    pbar = tqdm(total=total_training_tokens, desc="Training SAE")
    
    if training_run_state is None:
        train_contexts = [
            _build_train_context(sae, total_training_steps) for sae in sae_group
        ]
    else:
        train_contexts = training_run_state.train_contexts
        n_training_tokens = training_run_state.n_training_tokens
        n_training_steps = training_run_state.n_training_steps
        pbar.update(n_training_tokens)
        if not sae_group.cfg.resume:
            print("warning: you are passing in training run state but resume=False, is that intended?")
    
    if sae_group.cfg.resume:
        training_run_state.set_random_state()
    else:
        _init_sae_group_b_decs(sae_group, activation_store, all_layers)

    checkpoint_paths: list[str] = []

    class InterruptedException(Exception):
        pass

    def interrupt_callback(sig_num, stack_frame):
        raise InterruptedException() 

    try:
        signal.signal(signal.SIGINT, interrupt_callback)
        signal.signal(signal.SIGTERM, interrupt_callback)

        while n_training_tokens < total_training_tokens:
            # Do a training step.
            layer_acts = activation_store.next_batch()
            n_training_tokens += batch_size

            mse_losses: list[torch.Tensor] = []
            l1_losses: list[torch.Tensor] = []

            for (
                sparse_autoencoder,
                ctx,
            ) in zip(sae_group, train_contexts):
                wandb_suffix = _wandb_log_suffix(sae_group.cfg, sparse_autoencoder.cfg)
                step_output = _train_step(
                    sparse_autoencoder=sparse_autoencoder,
                    layer_acts=layer_acts,
                    ctx=ctx,
                    feature_sampling_window=feature_sampling_window,
                    use_wandb=use_wandb,
                    n_training_steps=n_training_steps,
                    all_layers=all_layers,
                    batch_size=batch_size,
                    wandb_suffix=wandb_suffix,
                )
                #if (n_training_steps % 50) == 0:
                #    print("tokens", n_training_tokens)
                mse_losses.append(step_output.mse_loss)
                l1_losses.append(step_output.l1_loss)
                if use_wandb:
                    with torch.no_grad():
                        if (n_training_steps + 1) % wandb_log_frequency == 0:
                            wandb.log(
                                _build_train_step_log_dict(
                                    sparse_autoencoder,
                                    step_output,
                                    ctx,
                                    wandb_suffix,
                                    n_training_tokens,
                                ),
                                step=n_training_steps,
                            )

                        # record loss frequently, but not all the time.
                        if n_training_steps == 0 or (n_training_steps + 1) % (wandb_log_frequency * 10) == 0:
                            sparse_autoencoder.eval()
                            run_evals(
                                sparse_autoencoder,
                                activation_store,
                                model,
                                n_training_steps,
                                suffix=wandb_suffix,
                            )
                            sparse_autoencoder.train()

            # checkpoint if at checkpoint frequency
            if n_training_steps % sae_group.cfg.checkpoint_every == 0:
                checkpoint_path = _save_checkpoint(
                    sae_group,
                    activation_store,
                    n_training_steps=n_training_steps,
                    n_training_tokens=n_training_tokens,
                    train_contexts=train_contexts,
                    checkpoint_name=n_training_tokens,
                ).path

            ###############

            n_training_steps += 1
            pbar.set_description(
                f"{n_training_steps}| MSE Loss {torch.stack(mse_losses).mean().item():.3f} | L1 {torch.stack(l1_losses).mean().item():.3f}"
            )
            pbar.update(batch_size)
    except (KeyboardInterrupt, InterruptedException):
        print("interrupted, saving progress")
        checkpoint_name = n_training_tokens
        checkpoint_path = _save_checkpoint(
            sae_group,
            activation_store,
            n_training_steps=n_training_steps,
            n_training_tokens=n_training_tokens,
            train_contexts=train_contexts,
            checkpoint_name=checkpoint_name,
        ).path
        print("done saving")
        raise
        
    # save final sae group to checkpoints folder
    final_checkpoint = _save_checkpoint(
        sae_group,
        activation_store,
        n_training_steps=n_training_steps,
        n_training_tokens=n_training_tokens,
        train_contexts=train_contexts,
        checkpoint_name=f"final_{n_training_tokens}",
        wandb_aliases=["final_model"],
    )
    checkpoint_paths.append(final_checkpoint.path)

    return TrainSAEGroupOutput(
        sae_group=sae_group,
        checkpoint_paths=checkpoint_paths,
        log_feature_sparsities=final_checkpoint.log_feature_sparsities,
    )


def _wandb_log_suffix(cfg: Any, hyperparams: Any):
    # Create a mapping from cfg list keys to their corresponding hyperparams attributes
    key_mapping = {
        "hook_point_layer": "layer",
        "l1_coefficient": "coeff",
        "lp_norm": "l",
        "lr": "lr",
    }

    # Generate the suffix by iterating over the keys that have list values in cfg
    suffix = "".join(
        f"_{key_mapping.get(key, key)}{getattr(hyperparams, key, '')}"
        for key, value in vars(cfg).items()
        if isinstance(value, list)
    )
    return suffix


def _build_train_context(
    sae: SparseAutoencoder, total_training_steps: int
) -> SAETrainContext:
    act_freq_scores = torch.zeros(
        cast(int, sae.cfg.d_sae),
        device=sae.cfg.device,
    )
    n_forward_passes_since_fired = torch.zeros(
        cast(int, sae.cfg.d_sae),
        device=sae.cfg.device,
    )
    n_frac_active_tokens = 0

    optimizer = Adam(sae.parameters(), lr=sae.cfg.lr)
    assert sae.cfg.lr_end is not None  # this is set in config post-init
    scheduler = get_scheduler(
        sae.cfg.lr_scheduler_name,
        lr=sae.cfg.lr,
        optimizer=optimizer,
        warm_up_steps=sae.cfg.lr_warm_up_steps,
        decay_steps=sae.cfg.lr_decay_steps,
        training_steps=total_training_steps,
        lr_end=sae.cfg.lr_end,
        num_cycles=sae.cfg.n_restart_cycles,
    )

    return SAETrainContext(
        act_freq_scores=act_freq_scores,
        n_forward_passes_since_fired=n_forward_passes_since_fired,
        n_frac_active_tokens=n_frac_active_tokens,
        optimizer=optimizer,
        scheduler=scheduler,
    )


def _init_sae_group_b_decs(
    sae_group: SAEGroup, activation_store: ActivationsStore, all_layers: list[int]
) -> None:
    """
    extract all activations at a certain layer and use for sae b_dec initialization
    """
    geometric_medians = {}
    for sae in sae_group:
        hyperparams = sae.cfg
        sae_layer_id = all_layers.index(hyperparams.hook_point_layer)
        if hyperparams.b_dec_init_method == "geometric_median":
            layer_acts = activation_store.storage_buffer.detach()[:, sae_layer_id, :]
            # get geometric median of the activations if we're using those.
            if sae_layer_id not in geometric_medians:
                median = compute_geometric_median(
                    layer_acts,
                    maxiter=100,
                ).median
                geometric_medians[sae_layer_id] = median
            sae.initialize_b_dec_with_precalculated(geometric_medians[sae_layer_id])
        elif hyperparams.b_dec_init_method == "mean":
            layer_acts = activation_store.storage_buffer.detach().cpu()[
                :, sae_layer_id, :
            ]
            sae.initialize_b_dec_with_mean(layer_acts)


@dataclass
class TrainStepOutput:
    sae_in: torch.Tensor
    sae_out: torch.Tensor
    feature_acts: torch.Tensor
    loss: torch.Tensor
    mse_loss: torch.Tensor
    l1_loss: torch.Tensor
    ghost_grad_loss: torch.Tensor
    ghost_grad_neuron_mask: torch.Tensor


def _train_step(
    sparse_autoencoder: SparseAutoencoder,
    layer_acts: torch.Tensor,
    ctx: SAETrainContext,
    feature_sampling_window: int,  # how many training steps between resampling the features / considiring neurons dead
    use_wandb: bool,
    n_training_steps: int,
    all_layers: list[int],
    batch_size: int,
    wandb_suffix: str,
) -> TrainStepOutput:
    assert sparse_autoencoder.cfg.d_sae is not None  # keep pyright happy
    hyperparams = sparse_autoencoder.cfg
    layer_id = all_layers.index(hyperparams.hook_point_layer)
    sae_in = layer_acts[:, layer_id, :]

    sparse_autoencoder.train()
    # Make sure the W_dec is still zero-norm
    sparse_autoencoder.set_decoder_norm_to_unit_norm()

    # log and then reset the feature sparsity every feature_sampling_window steps
    if (n_training_steps + 1) % feature_sampling_window == 0:
        feature_sparsity = ctx.feature_sparsity
        log_feature_sparsity = _log_feature_sparsity(feature_sparsity)

        if use_wandb:
            wandb_histogram = wandb.Histogram(log_feature_sparsity.numpy())
            wandb.log(
                {
                    f"metrics/mean_log10_feature_sparsity{wandb_suffix}": log_feature_sparsity.mean().item(),
                    f"plots/feature_density_line_chart{wandb_suffix}": wandb_histogram,
                    f"sparsity/below_1e-5{wandb_suffix}": (feature_sparsity < 1e-5)
                    .sum()
                    .item(),
                    f"sparsity/below_1e-6{wandb_suffix}": (feature_sparsity < 1e-6)
                    .sum()
                    .item(),
                },
                step=n_training_steps,
            )

        ctx.act_freq_scores = torch.zeros(
            sparse_autoencoder.cfg.d_sae, device=sparse_autoencoder.cfg.device
        )
        ctx.n_frac_active_tokens = 0

    ghost_grad_neuron_mask = (
        ctx.n_forward_passes_since_fired > sparse_autoencoder.cfg.dead_feature_window
    ).bool()

    # Forward and Backward Passes
    (
        sae_out,
        feature_acts,
        loss,
        mse_loss,
        l1_loss,
        ghost_grad_loss,
    ) = sparse_autoencoder(
        sae_in,
        ghost_grad_neuron_mask,
    )
    did_fire = (feature_acts > 0).float().sum(-2) > 0
    ctx.n_forward_passes_since_fired += 1
    ctx.n_forward_passes_since_fired[did_fire] = 0

    with torch.no_grad():
        # Calculate the sparsities, and add it to a list, calculate sparsity metrics
        ctx.act_freq_scores += (feature_acts.abs() > 0).float().sum(0)
        ctx.n_frac_active_tokens += batch_size
    
    ctx.optimizer.zero_grad()
    loss.backward()
    sparse_autoencoder.remove_gradient_parallel_to_decoder_directions()
    ctx.optimizer.step()
    ctx.scheduler.step()

    return TrainStepOutput(
        sae_in=sae_in,
        sae_out=sae_out,
        feature_acts=feature_acts,
        loss=loss,
        mse_loss=mse_loss,
        l1_loss=l1_loss,
        ghost_grad_loss=ghost_grad_loss,
        ghost_grad_neuron_mask=ghost_grad_neuron_mask,
    )


def _build_train_step_log_dict(
    sparse_autoencoder: SparseAutoencoder,
    output: TrainStepOutput,
    ctx: SAETrainContext,
    wandb_suffix: str,
    n_training_tokens: int,
) -> dict[str, Any]:
    sae_in = output.sae_in
    sae_out = output.sae_out
    feature_acts = output.feature_acts
    mse_loss = output.mse_loss
    l1_loss = output.l1_loss
    ghost_grad_loss = output.ghost_grad_loss
    loss = output.loss
    ghost_grad_neuron_mask = output.ghost_grad_neuron_mask

    # metrics for currents acts
    l0 = (feature_acts > 0).float().sum(-1).mean()
    current_learning_rate = ctx.optimizer.param_groups[0]["lr"]

    per_token_l2_loss = (sae_out - sae_in).pow(2).sum(dim=-1).squeeze()
    total_variance = (sae_in - sae_in.mean(0)).pow(2).sum(-1)
    explained_variance = 1 - per_token_l2_loss / total_variance

    return {
        # losses
        f"losses/mse_loss{wandb_suffix}": mse_loss.item(),
        f"losses/l1_loss{wandb_suffix}": l1_loss.item()
        / sparse_autoencoder.l1_coefficient,  # normalize by l1 coefficient
        f"losses/ghost_grad_loss{wandb_suffix}": ghost_grad_loss.item(),
        f"losses/overall_loss{wandb_suffix}": loss.item(),
        # variance explained
        f"metrics/explained_variance{wandb_suffix}": explained_variance.mean().item(),
        f"metrics/explained_variance_std{wandb_suffix}": explained_variance.std().item(),
        f"metrics/l0{wandb_suffix}": l0.item(),
        # sparsity
        f"sparsity/mean_passes_since_fired{wandb_suffix}": ctx.n_forward_passes_since_fired.mean().item(),
        f"sparsity/dead_features{wandb_suffix}": ghost_grad_neuron_mask.sum().item(),
        f"details/current_learning_rate{wandb_suffix}": current_learning_rate,
        "details/n_training_tokens": n_training_tokens,
    }


class SaveCheckpointOutput(NamedTuple):
    path: str
    activation_store_path: str
    training_run_state_path: str
    log_feature_sparsity_path: str
    log_feature_sparsities: list[torch.Tensor]


def resume_checkpoint(
    base_path: str,
    cfg: Any,
    model: HookedRootModule,
    batch_size: int,
    dataset: HfDataset | None = None,
    create_dataloader: bool = True,
) -> tuple[SAEGroup, ActivationsStore, SAETrainingRunState]:
    sae_group = SAEGroup.load_from_pretrained(f'{base_path}{SAVE_POSTFIX_SAE_GROUP}.pt')
    activation_store = ActivationsStore.load_from_pretrained(
        file_path=f'{base_path}{SAVE_POSTFIX_ACTIVATION_STORE}.pt',
        cfg=cfg,
        model=model,
        dataset=dataset,
        create_dataloader=create_dataloader
    )
    with open(f'{base_path}{SAVE_POSTFIX_TRAINING_STATE}.pt', 'rb') as f:
        training_run_state = pickle.load(f)

    total_training_tokens = sae_group.cfg.total_training_tokens
    total_training_steps = total_training_tokens // batch_size
    
    # the optimizers aren't attached to saes anymore, fix that
    # also the scheduler should be attached to them
    for ctx, sae in zip(training_run_state.train_contexts, sae_group.autoencoders):
        attached_context = _build_train_context(sae, total_training_steps=total_training_steps)
        attached_context.scheduler.load_state_dict(ctx.scheduler.state_dict())
        attached_context.optimizer.load_state_dict(ctx.optimizer.state_dict())
        del ctx.optimizer
        del ctx.scheduler
        ctx.optimizer = attached_context.optimizer
        ctx.scheduler = attached_context.scheduler
        # cleanup memory since that was two optimizers
        torch.cuda.empty_cache()

    # overwrite the cfg with a new cfg in case we want to change things
    sae_group.cfg = cfg
    activation_store.cfg = cfg
    # individual saes don't get new cfgs, maybe they should idk its messy bc of _init_autoencoders stuff

    return sae_group, activation_store, training_run_state


SAVE_POSTFIX_SAE_GROUP = ''
SAVE_POSTFIX_LOG_FEATURE_SPARSITY = '_log_feature_sparsity'
SAVE_POSTFIX_ACTIVATION_STORE = '_activation_store'
SAVE_POSTFIX_TRAINING_STATE = '_trainining_state'

def _save_checkpoint(
    sae_group: SAEGroup,
    activation_store: ActivationsStore,
    train_contexts: list[SAETrainContext],
    n_training_steps : int,
    n_training_tokens : int,
    checkpoint_name: int | str,
    wandb_aliases: list[str] | None = None,
) -> SaveCheckpointOutput:
    base_path = sae_group.cfg.get_base_path(checkpoint_name)
    path = (
        f'{base_path}{SAVE_POSTFIX_SAE_GROUP}.pt'
    )
    for sae in sae_group:
        sae.set_decoder_norm_to_unit_norm()
    sae_group.save_model(path)
    log_feature_sparsity_path = f"{base_path}{SAVE_POSTFIX_LOG_FEATURE_SPARSITY}.pt"
    log_feature_sparsities = [
        _log_feature_sparsity(ctx.feature_sparsity) for ctx in train_contexts
    ]
    torch.save(log_feature_sparsities, log_feature_sparsity_path)

    activation_store_path = f"{base_path}{SAVE_POSTFIX_ACTIVATION_STORE}.pt"
    activation_store.save(activation_store_path)

    training_run_state = SAETrainingRunState(
        train_contexts=train_contexts,
        n_training_steps=n_training_steps,
        n_training_tokens=n_training_tokens
    )
    
    training_run_state_path = f"{base_path}{SAVE_POSTFIX_TRAINING_STATE}.pt"
    with open(training_run_state_path, "wb") as f:
        pickle.dump(training_run_state, f)
    
    if sae_group.cfg.log_to_wandb:
        if str(checkpoint_name).startswith("final"):
            model_artifact = wandb.Artifact(
                f"{sae_group.get_name()}",
                type="model",
                metadata=dict(sae_group.cfg.__dict__),
            )
            model_artifact.add_file(path)
            wandb.log_artifact(model_artifact, aliases=wandb_aliases)

        sparsity_artifact = wandb.Artifact(
            f"{sae_group.get_name()}_log_feature_sparsity",
            type="log_feature_sparsity",
            metadata=dict(sae_group.cfg.__dict__),
        )
        sparsity_artifact.add_file(log_feature_sparsity_path)
        wandb.log_artifact(sparsity_artifact)
        # too large
        '''
        activation_store_artifact = wandb.Artifact(
            f"{sae_group.get_name()}_activation_store",
            type="activation_store",
            metadata=dict(sae_group.cfg.__dict__),
        )
        activation_store_artifact.add_file(activation_store_path)
        wandb.log_artifact(activation_store_artifact)
        # this is large because the optimizer state has a variable for every parameter
        training_run_state_artifact = wandb.Artifact(
            f"{sae_group.get_name()}_training_run_state",
            type="training_run_state",
            metadata=dict(sae_group.cfg.__dict__),
        )
        training_run_state_artifact.add_file(training_run_state_path)
        wandb.log_artifact(training_run_state_artifact)
        '''
    remove_excess_checkpoints(sae_group.cfg)
    return SaveCheckpointOutput(
        path=path,
        activation_store_path=activation_store_path,
        training_run_state_path=training_run_state_path,
        log_feature_sparsity_path=log_feature_sparsity_path,
        log_feature_sparsities=log_feature_sparsities
    )

def remove_excess_checkpoints(cfg : Any):
    checkpoints, is_done = cfg.get_checkpoints_by_step()
    sorted_keys = sorted(list(checkpoints.keys()))
    if not cfg.max_checkpoints is None:
        checkpoints_removing = sorted_keys[:-cfg.max_checkpoints]
        for k in checkpoints_removing:
            for f in checkpoints[k]:
                print(f"removing checkpoint file {k}")
                os.remove(f)

def _log_feature_sparsity(
    feature_sparsity: torch.Tensor, eps: float = 1e-10
) -> torch.Tensor:
    return torch.log10(feature_sparsity + eps).detach().cpu()
