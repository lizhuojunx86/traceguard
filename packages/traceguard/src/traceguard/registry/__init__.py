"""Model + prompt + replay-set registries (SPEC §4.2, §4.3, §3.4)."""
from traceguard.registry.models import NoEligibleModelError, register_model, select_model
from traceguard.registry.prompts import PromptTemplate, load_prompt
from traceguard.registry.replay import (
    add_replay_item,
    build_locked_replay_set,
    create_replay_set,
    lock_replay_set,
)

__all__ = [
    "NoEligibleModelError",
    "PromptTemplate",
    "add_replay_item",
    "build_locked_replay_set",
    "create_replay_set",
    "load_prompt",
    "lock_replay_set",
    "register_model",
    "select_model",
]
