# Quick note: one-line comment added as requested.
"""Central model defaults and fallback chains for each dev_assistant pipeline step."""

import os
from typing import Optional


STEP_MODELS = {
    "plan": "qwen/qwen3.6-plus:free",
    "specify_file_paths": "qwen/qwen3.6-plus:free",
    "generate_code": "qwen/qwen3.6-plus:free",
}

FALLBACK_MODELS = {
    "plan": "nvidia/nemotron-3-super-120b-a12b:free",
    "specify_file_paths": "nvidia/nemotron-3-super-120b-a12b:free",
    "generate_code": "nvidia/nemotron-3-super-120b-a12b:free",
}

FALLBACK_MODEL_CHAINS = {
    "plan": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "qwen/qwen3.6-plus:free",
    ],
    "specify_file_paths": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "qwen/qwen3.6-plus:free",
    ],
    "generate_code": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "qwen/qwen3.6-plus:free",
    ],
}


_FINETUNED_CODEGEN_MODEL: Optional[str] = None

_FINETUNED_ENV = os.getenv("FINETUNED_MODEL")
if _FINETUNED_ENV:
    try:
        from dev_assistant.training.model_selector import ModelSelector

        _selector = ModelSelector(_FINETUNED_ENV)
        _FINETUNED_CODEGEN_MODEL = _selector.get_codegen_model("index.py", STEP_MODELS["generate_code"])
        if _FINETUNED_CODEGEN_MODEL:
            print(f"Using fine-tuned model for code generation: {_FINETUNED_CODEGEN_MODEL}")
    except Exception:
        _FINETUNED_CODEGEN_MODEL = _FINETUNED_ENV


def _is_free_model(model: Optional[str]) -> bool:
    """Return True only for model identifiers explicitly marked free."""

    if not model:
        return False
    normalized = model.strip().lower()
    if normalized.endswith(":free"):
        return True
    finetuned_model = os.getenv("FINETUNED_MODEL", "").strip().lower()
    return bool(finetuned_model and normalized == finetuned_model)


def get_model(step: str, override: Optional[str] = None) -> str:
    """Return the override model for a step or the configured default."""

    if override and _is_free_model(override):
        return override
    if step == "generate_code" and _FINETUNED_CODEGEN_MODEL:
        return _FINETUNED_CODEGEN_MODEL
    return STEP_MODELS[step]


def get_model_candidates(step: str, primary: str) -> list[str]:
    """Return the ordered unique model candidates for a step."""

    chain = [primary]
    if step in FALLBACK_MODELS:
        chain.append(FALLBACK_MODELS[step])
    chain.extend(FALLBACK_MODEL_CHAINS.get(step, []))

    seen = set()
    ordered = []
    for model in chain:
        if model and _is_free_model(model) and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered
