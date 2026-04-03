# Quick note: one-line comment added as requested.
"""Generate, execute, and heal generated files until they succeed or retries are exhausted."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging

from dev_assistant.executor import SandboxMode, detect_language, execute_file, is_syntax_error
from dev_assistant.healer import heal_code
from dev_assistant.prompts import generate_code
from dev_assistant.utils import write_file


logger = logging.getLogger(__name__)


@dataclass
class HealingResult:
    """Summary of one generate/execute/heal cycle."""

    file_path: str
    final_code: str
    succeeded: bool
    attempts: int
    final_error: str | None
    execution_skipped: bool


def _resolve_output_file(output_dir: str, file_path: str) -> Path:
    if ".." in Path(file_path).parts:
        raise ValueError("Refusing to write generated files with '..' in the path")
    return Path(output_dir) / file_path


async def generate_and_heal(
    file_path: str,
    shared_dependencies: str,
    prompt: str,
    output_dir: str,
    generate_model: str,
    heal_model: str,
    max_heal_attempts: int = 3,
    heal: bool = True,
    execute: bool = True,
    debug: bool = False,
    sandbox_mode: SandboxMode | str = SandboxMode.AUTO,
    sandbox_network: bool = False,
) -> HealingResult:
    """Generate a file, execute it, and optionally heal syntax failures."""

    output_file = _resolve_output_file(output_dir, file_path)
    code = await generate_code(prompt, shared_dependencies, file_path, model=generate_model)
    write_file(str(output_file), code)

    if not execute:
        return HealingResult(
            file_path=file_path,
            final_code=code,
            succeeded=True,
            attempts=0,
            final_error=None,
            execution_skipped=True,
        )

    language = detect_language(file_path)
    if language == "unknown":
        return HealingResult(
            file_path=file_path,
            final_code=code,
            succeeded=True,
            attempts=0,
            final_error=None,
            execution_skipped=True,
        )

    execution_result = execute_file(
        file_path,
        cwd=output_dir,
        sandbox_mode=sandbox_mode,
        sandbox_network=sandbox_network,
    )
    if execution_result.success:
        return HealingResult(
            file_path=file_path,
            final_code=code,
            succeeded=True,
            attempts=0,
            final_error=None,
            execution_skipped=False,
        )

    if not heal:
        return HealingResult(
            file_path=file_path,
            final_code=code,
            succeeded=False,
            attempts=0,
            final_error=execution_result.stderr or "Execution failed",
            execution_skipped=False,
        )

    if not is_syntax_error(execution_result):
        if debug:
            logger.info("Skipping healing for %s due to non-syntax error: %s", file_path, execution_result.stderr)
        return HealingResult(
            file_path=file_path,
            final_code=code,
            succeeded=False,
            attempts=0,
            final_error=execution_result.stderr or "Execution failed",
            execution_skipped=True,
        )

    current_code = code
    last_error = execution_result.stderr or "Execution failed"
    for attempt in range(1, max_heal_attempts + 1):
        fixed_code = await heal_code(
            file_path=file_path,
            broken_code=current_code,
            error_output=last_error,
            original_prompt=prompt,
            shared_dependencies=shared_dependencies,
            model=heal_model,
            attempt=attempt,
            max_attempts=max_heal_attempts,
        )
        current_code = fixed_code
        write_file(str(output_file), fixed_code)
        execution_result = execute_file(
            file_path,
            cwd=output_dir,
            sandbox_mode=sandbox_mode,
            sandbox_network=sandbox_network,
        )
        if execution_result.success:
            return HealingResult(
                file_path=file_path,
                final_code=fixed_code,
                succeeded=True,
                attempts=attempt,
                final_error=None,
                execution_skipped=False,
            )
        last_error = execution_result.stderr or last_error

    return HealingResult(
        file_path=file_path,
        final_code=current_code,
        succeeded=False,
        attempts=max_heal_attempts,
        final_error=last_error,
        execution_skipped=False,
    )
