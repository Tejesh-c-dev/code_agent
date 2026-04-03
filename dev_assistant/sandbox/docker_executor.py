# Quick note: one-line comment added as requested.
"""Docker-backed sandbox executor for generated code."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

from dev_assistant.execution_types import ExecutionResult


LANGUAGE_IMAGES = {
    "python": "python:3.11-slim",
    "javascript": "node:18-slim",
    "typescript": "node:18-slim",
    "shell": "bash:5.2",
}


LANGUAGE_COMMANDS = {
    "python": ["python"],
    "javascript": ["node"],
    "typescript": ["npx", "ts-node"],
    "shell": ["bash"],
}


LANGUAGE_EXTENSIONS = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "shell": "sh",
}


class DockerSandbox:
    """Execute generated source code in an isolated Docker container."""

    def __init__(
        self,
        image: str,
        timeout: int = 30,
        memory_limit: str = "256m",
        cpu_limit: float = 0.5,
        network_disabled: bool = True,
    ) -> None:
        self.image = image
        self.timeout = timeout
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.network_disabled = network_disabled

    async def _run_container(self, command: list[str], timeout: int) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return -1, "", f"Execution timed out after {timeout} seconds"

    @staticmethod
    async def _docker_info() -> int:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait()

    @staticmethod
    def _run_coro_sync(coro):
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if not in_running_loop:
            return asyncio.run(coro)

        result: dict[str, object] = {}
        error: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover
                error["exc"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()

        if "exc" in error:
            raise error["exc"]
        return result.get("value")

    def execute(self, code: str, language: str, timeout: int = 30) -> ExecutionResult:
        """Run code in Docker and return structured execution output."""

        if language not in LANGUAGE_IMAGES or language not in LANGUAGE_COMMANDS:
            return ExecutionResult(
                success=True,
                stdout="",
                stderr="",
                exit_code=0,
                language=language,
                file_path="",
            )

        file_ext = LANGUAGE_EXTENSIONS.get(language, "txt")
        command_runner = LANGUAGE_COMMANDS[language]
        image = self.image or LANGUAGE_IMAGES[language]
        effective_timeout = timeout or self.timeout

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=f".{file_ext}", delete=False, encoding="utf-8") as handle:
                handle.write(code)
                temp_path = Path(handle.name)

            code_mount = f"{temp_path.resolve()}:/code/main.{file_ext}:ro"
            network_mode = "none" if self.network_disabled else "bridge"
            docker_command = [
                "docker",
                "run",
                "--rm",
                f"--memory={self.memory_limit}",
                f"--cpus={self.cpu_limit}",
                f"--network={network_mode}",
                "--read-only",
                "--tmpfs",
                "/tmp:size=64m",
                "-v",
                code_mount,
                image,
                *command_runner,
                f"/code/main.{file_ext}",
            ]

            return_code, stdout, stderr = self._run_coro_sync(self._run_container(docker_command, timeout=effective_timeout))
            return ExecutionResult(
                success=return_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=return_code,
                language=language,
                file_path=str(temp_path),
            )
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def is_available() -> bool:
        """Return True when Docker is installed and daemon is reachable."""

        try:
            return_code = DockerSandbox._run_coro_sync(DockerSandbox._docker_info())
            return return_code == 0
        except Exception:
            return False
