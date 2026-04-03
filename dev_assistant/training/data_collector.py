# Quick note: one-line comment added as requested.
"""Collect high-quality generation examples for fine-tuning."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dev_assistant.db.database import GenerationJob


@dataclass(slots=True)
class TrainingExample:
    job_id: str
    prompt: str
    plan: str
    file_paths: list[str]
    file_contents: dict[str, str]
    model_used: str
    quality_score: float
    heal_attempts: int = 0
    files_generated: int = 0


def score_example(example: TrainingExample) -> float:
    """Heuristic quality score from 0.0 to 1.0."""

    score = 0.0
    if example.files_generated >= 5:
        score += 0.2
    if example.heal_attempts == 0:
        score += 0.2
    if any(Path(path).name.lower() == "readme.md" for path in example.file_paths):
        score += 0.2
    if any(Path(path).name.startswith("test_") or Path(path).name.endswith(".test.js") for path in example.file_paths):
        score += 0.2
    all_non_empty = True
    for content in example.file_contents.values():
        if not content.strip() or len(content.splitlines()) <= 10:
            all_non_empty = False
            break
    if all_non_empty and example.file_contents:
        score += 0.2
    return min(score, 1.0)


async def collect_training_examples(
    db: AsyncSession,
    min_files: int = 3,
    max_heal_attempts: int = 1,
    limit: int = 500,
) -> list[TrainingExample]:
    """Collect completed generation jobs whose outputs still exist on disk."""

    query = (
        select(GenerationJob)
        .where(GenerationJob.status == "completed")
        .where(GenerationJob.files_generated >= min_files)
        .order_by(GenerationJob.created_at.desc())
        .limit(limit)
    )
    if hasattr(GenerationJob, "heal_attempts"):
        query = query.where(GenerationJob.heal_attempts <= max_heal_attempts)

    result = await db.execute(query)
    jobs = result.scalars().all()

    examples: list[TrainingExample] = []
    for job in jobs:
        output_dir = Path(job.output_dir)
        if not output_dir.exists():
            continue

        plan_file = output_dir / "shared_deps.md"
        plan_text = plan_file.read_text(encoding="utf-8", errors="replace") if plan_file.exists() else ""

        file_paths: list[str] = []
        file_contents: dict[str, str] = {}
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name in {"shared_deps.md"} or path.name.startswith(".git"):
                continue
            relative_path = path.relative_to(output_dir).as_posix()
            file_paths.append(relative_path)
            file_contents[relative_path] = path.read_text(encoding="utf-8", errors="replace")

        example = TrainingExample(
            job_id=job.id,
            prompt=job.prompt,
            plan=plan_text,
            file_paths=file_paths,
            file_contents=file_contents,
            model_used=job.model,
            heal_attempts=getattr(job, "heal_attempts", 0) or 0,
            files_generated=len(file_paths),
            quality_score=0.0,
        )
        example.quality_score = score_example(example)
        examples.append(example)

    return examples
