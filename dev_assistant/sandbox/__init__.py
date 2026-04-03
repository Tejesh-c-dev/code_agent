# Quick note: one-line comment added as requested.
"""Sandbox execution utilities for isolated code runs."""

from dev_assistant.sandbox.docker_executor import DockerSandbox

__all__ = ["DockerSandbox", "execute_file"]


def execute_file(*args, **kwargs):
	"""Late import to avoid executor/sandbox circular imports."""

	from dev_assistant.executor import execute_file as _execute_file

	return _execute_file(*args, **kwargs)
