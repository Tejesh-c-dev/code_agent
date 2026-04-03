# Quick note: one-line comment added as requested.
"""Standalone CLI for collecting data and managing fine-tunes."""

from __future__ import annotations

import argparse
import asyncio
import pickle
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from dev_assistant.db.database import AsyncSessionLocal, init_db
from dev_assistant.training.data_collector import collect_training_examples
from dev_assistant.training.dataset_builder import build_finetune_dataset
from dev_assistant.training.finetune_manager import FineTuneManager
from dev_assistant.training.model_selector import ModelSelector


TRAINING_DIR = Path("training_data")
EXAMPLES_PATH = TRAINING_DIR / "examples.pkl"
TRAIN_PATH = TRAINING_DIR / "train.jsonl"
VAL_PATH = TRAINING_DIR / "val.jsonl"


def _estimate_cost_from_jsonl(path: Path) -> float:
    if not path.exists():
        return 0.0
    text = path.read_text(encoding="utf-8", errors="replace")
    approx_tokens = max(len(text) // 4, 1)
    return approx_tokens * 0.008 / 1000


async def _collect() -> None:
    await init_db()
    async with AsyncSessionLocal() as session:
        examples = await collect_training_examples(session)
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    with EXAMPLES_PATH.open("wb") as handle:
        pickle.dump(examples, handle)
    print(f"Collected {len(examples)} examples -> {EXAMPLES_PATH}")


def _load_examples() -> list:
    if not EXAMPLES_PATH.exists():
        raise FileNotFoundError(f"Missing {EXAMPLES_PATH}; run collect first")
    with EXAMPLES_PATH.open("rb") as handle:
        return pickle.load(handle)


def _build(min_quality: float) -> None:
    examples = _load_examples()
    build_finetune_dataset(examples, str(TRAINING_DIR), min_quality_score=min_quality)


def _finetune() -> None:
    if not TRAIN_PATH.exists() or not VAL_PATH.exists():
        raise FileNotFoundError("Missing train.jsonl or val.jsonl; run build first")

    examples = _load_examples()
    if len(examples) < 10:
      raise ValueError("At least 10 training examples are required before fine-tuning")

    train_cost = _estimate_cost_from_jsonl(TRAIN_PATH)
    val_cost = _estimate_cost_from_jsonl(VAL_PATH)
    estimated_cost = train_cost + val_cost
    answer = input(f"Estimated fine-tune cost: ~${estimated_cost:.2f}. Proceed? [y/n] ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Cancelled.")
        return

    manager = FineTuneManager()
    train_id = manager.upload_dataset(str(TRAIN_PATH))
    val_id = manager.upload_dataset(str(VAL_PATH))
    job_id = manager.start_finetune(train_id, val_id)
    print(f"Started fine-tune job: {job_id}")
    model_name = manager.wait_for_completion(job_id)
    print(f"Fine-tuned model ready: {model_name}")


def _status(job_id: str) -> None:
    manager = FineTuneManager()
    print(manager.check_status(job_id))


def _evaluate() -> None:
    selector = ModelSelector()
    print(selector.evaluate_finetuned([
        "Create a responsive todo app",
        "Build a markdown previewer",
        "Make a simple pong game",
    ]))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m dev_assistant.training.train")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("collect")

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--min-quality", type=float, default=0.6)

    subparsers.add_parser("finetune")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--job-id", required=True)

    subparsers.add_parser("evaluate")

    args = parser.parse_args()

    if args.command == "collect":
        asyncio.run(_collect())
    elif args.command == "build":
        _build(args.min_quality)
    elif args.command == "finetune":
        _finetune()
    elif args.command == "status":
        _status(args.job_id)
    elif args.command == "evaluate":
        _evaluate()


if __name__ == "__main__":
    main()
