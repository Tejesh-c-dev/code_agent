# Quick note: one-line comment added as requested.
"""Shared execution result types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExecutionResult:
    """Structured result from executing a generated file."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int
    language: str
    file_path: str
