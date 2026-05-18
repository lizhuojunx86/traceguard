"""Model + prompt registries (SPEC §4.2, §4.3)."""
from traceguard.registry.models import NoEligibleModelError, register_model, select_model
from traceguard.registry.prompts import PromptTemplate, load_prompt

__all__ = [
    "NoEligibleModelError",
    "PromptTemplate",
    "load_prompt",
    "register_model",
    "select_model",
]
