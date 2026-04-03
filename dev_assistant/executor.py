# Quick note: one-line comment added as requested.
"""Safe execution helpers for generated code files."""

from __future__ import annotations

from enum import Enum
import logging
from pathlib import Path
import subprocess
from typing import Optional

from dev_assistant.execution_types import ExecutionResult
from dev_assistant.sandbox.docker_executor import DockerSandbox, LANGUAGE_IMAGES
from dev_assistant.sandbox.sandbox_config import SANDBOX_SETTINGS


logger = logging.getLogger(__name__)


class SandboxMode(str, Enum):
    """Supported execution backends."""

    DOCKER = "docker"
    SUBPROCESS = "subprocess"
    AUTO = "auto"


def detect_language(file_path: str) -> str:
    """Infer the runtime language from the file extension."""

    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".jsx"}:
        return "javascript"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    if suffix in {".sh", ".bash"}:
        return "shell"
    return "unknown"


def _resolve_executable_path(file_path: str, cwd: str) -> Path:
    cwd_path = Path(cwd).resolve()
    candidate = Path(file_path)

    if any(part == ".." for part in candidate.parts):
        raise ValueError("Refusing to execute paths containing '..'")

    resolved = candidate if candidate.is_absolute() else (cwd_path / candidate)
    resolved = resolved.resolve(strict=False)

    try:
        resolved.relative_to(cwd_path)
    except ValueError as exc:
        raise ValueError("Refusing to execute files outside the cwd directory") from exc

    return resolved


def _build_subprocess_command(language: str, resolved_path: Path) -> list[str]:
    if language == "python":
        return ["python", str(resolved_path)]
    if language in {"javascript", "typescript"}:
        return ["node", str(resolved_path)]
    if language == "shell":
        return ["bash", str(resolved_path)]
    return []


def _execute_subprocess(file_path: str, cwd: str, timeout: int, language: str, resolved_path: Path) -> ExecutionResult:
    command = _build_subprocess_command(language, resolved_path)
    if not command:
        return ExecutionResult(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
            language=language,
            file_path=str(resolved_path),
        )

    try:
        completed_process = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ExecutionResult(
            success=completed_process.returncode == 0,
            stdout=completed_process.stdout or "",
            stderr=completed_process.stderr or "",
            exit_code=completed_process.returncode,
            language=language,
            file_path=str(resolved_path),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timeout_message = f"Execution timed out after {timeout} seconds"
        if stderr:
            stderr = f"{stderr}\n{timeout_message}"
        else:
            stderr = timeout_message
        return ExecutionResult(
            success=False,
            stdout=stdout,
            stderr=stderr,
            exit_code=-1,
            language=language,
            file_path=str(resolved_path),
        )


def execute_file(
    file_path: str,
    cwd: str,
    timeout: int = 30,
    sandbox_mode: SandboxMode | str = SandboxMode.AUTO,
    sandbox_network: bool | None = None,
) -> ExecutionResult:
    """Execute a generated file with a timeout and capture stdout/stderr."""

    candidate = Path(file_path)
    if any(part == ".." for part in candidate.parts):
        raise ValueError("Refusing to execute paths containing '..'")

    language = detect_language(file_path)
    if language == "unknown":
        return ExecutionResult(
            success=True,
            stdout="",
            stderr="",
            exit_code=0,
            language=language,
            file_path=file_path,
        )

    resolved_path = _resolve_executable_path(file_path, cwd)
    if not resolved_path.exists():
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"File not found: {resolved_path}",
            exit_code=1,
            language=language,
            file_path=str(resolved_path),
        )

    if isinstance(sandbox_mode, SandboxMode):
        resolved_mode = sandbox_mode
    else:
        resolved_mode = SandboxMode(str(sandbox_mode).lower())
    if resolved_mode == SandboxMode.AUTO:
        resolved_mode = SandboxMode(str(SANDBOX_SETTINGS.get("mode", "auto")).lower())
        if resolved_mode == SandboxMode.AUTO:
            resolved_mode = SandboxMode.DOCKER if DockerSandbox.is_available() else SandboxMode.SUBPROCESS

    if resolved_mode == SandboxMode.DOCKER:
        if DockerSandbox.is_available():
            effective_timeout = timeout or SANDBOX_SETTINGS.get("timeout_seconds", 30)
            network_disabled = SANDBOX_SETTINGS.get("network_disabled", True)
            if sandbox_network is not None:
                network_disabled = not sandbox_network
            sandbox = DockerSandbox(
                image=LANGUAGE_IMAGES.get(language, "python:3.11-slim"),
                timeout=effective_timeout,
                memory_limit=SANDBOX_SETTINGS.get("memory_limit", "256m"),
                cpu_limit=float(SANDBOX_SETTINGS.get("cpu_limit", 0.5)),
                network_disabled=bool(network_disabled),
            )
            code = resolved_path.read_text(encoding="utf-8", errors="replace")
            result = sandbox.execute(code=code, language=language, timeout=effective_timeout)
            result.file_path = str(resolved_path)
            return result

        logger.warning("Docker not available, falling back to subprocess execution (unsafe)")
        return _execute_subprocess(file_path, cwd, timeout, language, resolved_path)

    if resolved_mode == SandboxMode.AUTO:
        if DockerSandbox.is_available():
            return execute_file(file_path=file_path, cwd=cwd, timeout=timeout, sandbox_mode=SandboxMode.DOCKER, sandbox_network=sandbox_network)
        logger.warning("Docker not available, falling back to subprocess execution (unsafe)")
        return _execute_subprocess(file_path, cwd, timeout, language, resolved_path)

    if resolved_mode == SandboxMode.SUBPROCESS:
        logger.warning("Running in subprocess mode (unsafe)")
    return _execute_subprocess(file_path, cwd, timeout, language, resolved_path)


def is_syntax_error(result: ExecutionResult) -> bool:
    """Heuristically determine whether stderr reflects a syntax bug."""

    stderr = (result.stderr or "").lower()
    if not stderr:
        return False

    missing_module_markers = (
        "modulenotfounderror",
        "no module named",
        "cannot find module",
    )
    if any(marker in stderr for marker in missing_module_markers):
        return False

    syntax_markers = (
        "syntaxerror",
        "indentationerror",
        "referenceerror",
        "unexpected token",
        "unexpected identifier",
        "unterminated string",
        "unterminated template",
        "eof while parsing",
        "unexpected end of input",
    )
    return any(marker in stderr for marker in syntax_markers)
