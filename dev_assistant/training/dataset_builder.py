# Quick note: one-line comment added as requested.
"""Build OpenAI fine-tuning datasets from collected training examples."""

from __future__ import annotations

import json
from pathlib import Path
from random import Random
from typing import Iterable

from dev_assistant.training.data_collector import TrainingExample


SYSTEM_PLAN = "You are an expert software architect who writes concise, actionable development plans."
SYSTEM_CODE = "You are an expert software engineer who writes production-quality code."


def _validate_json_line(entry: dict) -> str:
    line = json.dumps(entry, ensure_ascii=False)
    json.loads(line)
    return line


def _plan_entry(example: TrainingExample) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PLAN},
            {"role": "user", "content": f"Create a development plan for: {example.prompt}"},
            {"role": "assistant", "content": example.plan},
        ]
    }


def _code_entries(example: TrainingExample) -> Iterable[dict]:
    shared_deps = example.plan
    for file_path, file_code in example.file_contents.items():
        yield {
            "messages": [
                {"role": "system", "content": SYSTEM_CODE},
                {
                    "role": "user",
                    "content": f"Generate {file_path} for this project:\n{example.prompt}\n\nShared deps:\n{shared_deps}",
                },
                {"role": "assistant", "content": file_code},
            ]
        }


def build_finetune_dataset(
    examples: list[TrainingExample],
    output_path: str,
    min_quality_score: float = 0.6,
    split: float = 0.9,
) -> tuple[str, str]:
    """Write train.jsonl and val.jsonl from the filtered examples."""

    filtered = [example for example in examples if example.quality_score >= min_quality_score]
    if not filtered:
        raise ValueError("No training examples met the minimum quality threshold")

    rng = Random(42)
    rng.shuffle(filtered)

    entries: list[dict] = []
    for example in filtered:
        entries.append(_plan_entry(example))
        entries.extend(_code_entries(example))

    train_count = max(1, int(len(entries) * split))
    train_entries = entries[:train_count]
    val_entries = entries[train_count:]

    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    train_path = output_dir / "train.jsonl"
    val_path = output_dir / "val.jsonl"

    with train_path.open("w", encoding="utf-8") as train_file:
        for entry in train_entries:
            train_file.write(_validate_json_line(entry) + "\n")

    with val_path.open("w", encoding="utf-8") as val_file:
        for entry in val_entries:
            val_file.write(_validate_json_line(entry) + "\n")

    print(f"Built dataset: {len(train_entries)} train, {len(val_entries)} val examples")
    return str(train_path), str(val_path)
