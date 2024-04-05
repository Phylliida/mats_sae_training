from typing import Any, cast

import wandb
import traceback
from sae_training.config import LanguageModelSAERunnerConfig

# from sae_training.activation_store import ActivationStore
from sae_training.train_sae_on_language_model import train_sae_group_on_language_model, resume_checkpoint
from sae_training.utils import LMSparseAutoencoderSessionloader


def language_model_sae_runner(cfg: LanguageModelSAERunnerConfig):
    """ """

    if cfg.resume:
        try:
            base_path = cfg.get_resume_base_path()
            model = cfg.model_class.from_pretrained(cfg.model_name)
            model.to(cfg.device)
            sparse_autoencoder, activations_loader, training_run_state = resume_checkpoint(
                base_path=base_path,
                cfg=cfg,
                model=model
            )
        except FileNotFoundError:
            print(traceback.format_exc())
            print("failed to find checkpoint to resume from, setting resume to False")
            cfg.resume = False
    
    if not cfg.resume:
        training_run_state = None
        if cfg.from_pretrained_path is not None:
            (
                model,
                sparse_autoencoder,
                activations_loader,
            ) = LMSparseAutoencoderSessionloader.load_session_from_pretrained(
                cfg.from_pretrained_path
            )
            cfg = sparse_autoencoder.cfg
        else:
            loader = LMSparseAutoencoderSessionloader(cfg)
            model, sparse_autoencoder, activations_loader = loader.load_session()

    if cfg.log_to_wandb:
        wandb.init(project=cfg.wandb_project, config=cast(Any, cfg), name=cfg.run_name, id=cfg.wandb_id)
    
    # train SAE
    sparse_autoencoder = train_sae_group_on_language_model(
        model,
        sparse_autoencoder,
        activations_loader,
        training_run_state=training_run_state,
        batch_size=cfg.train_batch_size,
        feature_sampling_window=cfg.feature_sampling_window,
        use_wandb=cfg.log_to_wandb,
        wandb_log_frequency=cfg.wandb_log_frequency,
    )

    if cfg.log_to_wandb:
        wandb.finish()

    return sparse_autoencoder
