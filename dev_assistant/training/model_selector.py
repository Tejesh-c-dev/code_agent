# Quick note: one-line comment added as requested.
"""Model selection helpers for fine-tuned code generation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dev_assistant.prompts import generate_code


class ModelSelector:
    """Choose between a fine-tuned model and fallback models."""

    def __init__(self, finetuned_model: str | None = None) -> None:
        self.finetuned_model = finetuned_model if finetuned_model is not None else os.getenv("FINETUNED_MODEL")

    def get_codegen_model(self, file_path: str, fallback_model: str) -> str:
        if self.finetuned_model and Path(file_path).suffix.lower() in {".py", ".js", ".ts", ".jsx", ".tsx"}:
            return self.finetuned_model
        return fallback_model

    def evaluate_finetuned(self, test_prompts: list[str]) -> dict:
        base_wins = 0
        finetuned_wins = 0
        prompts_to_run = test_prompts[:3]
        fallback_model = os.getenv("FALLBACK_CODEGEN_MODEL", "qwen/qwen3.6-plus:free")

        for prompt in prompts_to_run:
            base_output = asyncio.run(generate_code(prompt, "shared deps", "index.js", model=fallback_model))
            fine_output = base_output
            if self.finetuned_model:
                fine_output = asyncio.run(generate_code(prompt, "shared deps", "index.js", model=self.finetuned_model))

            if len(fine_output) >= len(base_output):
                finetuned_wins += 1
            else:
                base_wins += 1

        recommendation = "use_finetuned" if finetuned_wins >= base_wins and self.finetuned_model else "use_base"
        return {"finetuned_wins": finetuned_wins, "base_wins": base_wins, "recommendation": recommendation}
