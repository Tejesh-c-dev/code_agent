# Quick note: one-line comment added as requested.
"""OpenAI fine-tuning helpers for dev_assistant."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import openai


HISTORY_FILE = Path("training_data/finetune_history.json")


def _append_history(entry: dict[str, Any]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.append(entry)
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


class FineTuneManager:
    """Wrapper around OpenAI fine-tuning APIs with compatibility fallbacks."""

    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            openai.api_key = api_key

    def upload_dataset(self, jsonl_path: str) -> str:
        with open(jsonl_path, "rb") as dataset_file:
            file_obj = openai.File.create(file=dataset_file, purpose="fine-tune")
        file_id = file_obj["id"] if isinstance(file_obj, dict) else file_obj.id
        _append_history({"event": "upload", "jsonl_path": jsonl_path, "file_id": file_id, "timestamp": time.time()})
        return file_id

    def start_finetune(
        self,
        train_file_id: str,
        val_file_id: str,
        base_model: str = "gpt-4o-mini-2024-07-18",
        suffix: str = "dev-assistant",
    ) -> str:
        try:
            job = openai.FineTuningJob.create(
                training_file=train_file_id,
                validation_file=val_file_id,
                model=base_model,
                suffix=suffix,
            )
            job_id = job["id"] if isinstance(job, dict) else job.id
        except AttributeError:
            job = openai.FineTune.create(training_file=train_file_id, validation_file=val_file_id, model=base_model, suffix=suffix)
            job_id = job["id"] if isinstance(job, dict) else job.id
        _append_history({"event": "start", "job_id": job_id, "base_model": base_model, "timestamp": time.time()})
        return job_id

    def check_status(self, job_id: str) -> dict:
        try:
            job = openai.FineTuningJob.retrieve(job_id)
            payload = job if isinstance(job, dict) else job.__dict__
            status = payload.get("status")
            fine_tuned_model = payload.get("fine_tuned_model")
            trained_tokens = payload.get("trained_tokens") or payload.get("estimated_finish")
            error = payload.get("error")
        except AttributeError:
            job = openai.FineTune.retrieve(job_id)
            payload = job if isinstance(job, dict) else job.__dict__
            status = payload.get("status")
            fine_tuned_model = payload.get("fine_tuned_model")
            trained_tokens = payload.get("trained_tokens")
            error = payload.get("error")
        return {
            "status": status,
            "fine_tuned_model": fine_tuned_model,
            "trained_tokens": trained_tokens,
            "error": error,
        }

    def wait_for_completion(self, job_id: str, poll_interval: int = 60) -> str:
        while True:
            status = self.check_status(job_id)
            print(f"Fine-tune job {job_id}: {status['status']}")
            if status["status"] == "succeeded":
                model_name = status["fine_tuned_model"]
                _append_history({"event": "succeeded", "job_id": job_id, "model": model_name, "timestamp": time.time()})
                return model_name
            if status["status"] == "failed":
                _append_history({"event": "failed", "job_id": job_id, "error": status["error"], "timestamp": time.time()})
                raise RuntimeError(status["error"] or f"Fine-tune job {job_id} failed")
            time.sleep(poll_interval)
