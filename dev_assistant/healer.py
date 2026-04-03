# Quick note: one-line comment added as requested.
"""LLM prompt helpers for repairing generated code after execution failures."""

from __future__ import annotations

import re
from typing import Optional

from dev_assistant.openrouter_client import OpenRouterCompletion


def build_fix_prompt(
    original_prompt: str,
    file_path: str,
    broken_code: str,
    error_output: str,
    attempt: int,
    max_attempts: int,
    shared_dependencies: str,
) -> str:
    """Build a detailed prompt asking the model to fix the failing file."""

    return f"""You are fixing a generated code file for dev_assistant.

Original task:
{original_prompt}

File path:
{file_path}

Shared dependencies:
{shared_dependencies}

Attempt:
{attempt} of {max_attempts}

The code currently on disk is:
{broken_code}

The exact runtime or syntax error is:
{error_output}

Fix the file so it runs correctly and stays faithful to the original task.
Return ONLY the fixed code.
Do not include markdown fences.
Do not include any explanation.
Do not include any preamble.
"""


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from an LLM response."""

    fenced_match = re.search(r"```[\w+-]*\n([\s\S]*?)```", text)
    if fenced_match:
        return fenced_match.group(1).strip()
    return text.strip().strip("`").strip()


async def heal_code(
    file_path: str,
    broken_code: str,
    error_output: str,
    original_prompt: str,
    shared_dependencies: str,
    model: str,
    attempt: int,
    max_attempts: int,
) -> str:
    """Ask the LLM to repair a code file and return the fixed code."""

    prompt = build_fix_prompt(
        original_prompt=original_prompt,
        file_path=file_path,
        broken_code=broken_code,
        error_output=error_output,
        attempt=attempt,
        max_attempts=max_attempts,
        shared_dependencies=shared_dependencies,
    )

    messages = [
        {
            "role": "system",
            "content": "You repair generated source code. Return only the corrected code.",
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    response = OpenRouterCompletion.create(
        model=model,
        messages=messages,
        temperature=0.2,
        stream=False,
        step="generate_code",
    )
    content = response["choices"][0]["message"]["content"]
    return _strip_code_fences(content)
