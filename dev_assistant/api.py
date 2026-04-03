# Quick note: one-line comment added as requested.
"""Agent Protocol adapter updated to use the configured per-step models."""

import enum
import os
from pathlib import Path

from agent_protocol import Agent, Step, Task

from dev_assistant.model_config import get_model
from dev_assistant.prompts import generate_code, plan, specify_file_paths
from dev_assistant.utils import write_file


class StepTypes(str, enum.Enum):
    PLAN = "plan"
    SPECIFY_FILE_PATHS = "specify_file_paths"
    GENERATE_CODE = "generate_code"


async def _generate_shared_deps(step: Step) -> Step:
    task = await Agent.db.get_task(step.task_id)
    shared_deps = plan(task.input, model=get_model("plan"))
    await Agent.db.create_step(
        step.task_id,
        StepTypes.SPECIFY_FILE_PATHS,
        additional_properties={
            "shared_deps": shared_deps,
        },
    )
    step.output = shared_deps
    return step


async def _generate_file_paths(task: Task, step: Step) -> Step:
    shared_deps = step.additional_properties["shared_deps"]
    file_paths = specify_file_paths(task.input, shared_deps, model=get_model("specify_file_paths"))
    for file_path in file_paths[:-1]:
        await Agent.db.create_step(
            task.task_id,
            f"Generate code for {file_path}",
            additional_properties={
                "shared_deps": shared_deps,
                "file_path": file_paths[-1],
            },
        )

    await Agent.db.create_step(
        task.task_id,
        f"Generate code for {file_paths[-1]}",
        is_last=True,
        additional_properties={
            "shared_deps": shared_deps,
            "file_path": file_paths[-1],
        },
    )

    step.output = f"File paths are: {str(file_paths)}"
    return step


async def _generate_code(task: Task, step: Step) -> Step:
    shared_deps = step.additional_properties["shared_deps"]
    file_path = step.additional_properties["file_path"]

    code = await generate_code(task.input, shared_deps, file_path, model=get_model("generate_code"))
    step.output = code

    write_file(os.path.join(Agent.get_workspace(task.task_id), file_path), code)
    path = Path("./" + file_path)
    await Agent.db.create_artifact(
        task_id=task.task_id,
        step_id=step.step_id,
        relative_path=str(path.parent),
        file_name=path.name,
    )

    return step


async def task_handler(task: Task) -> None:
    if not task.input:
        raise Exception("No task prompt")
    await Agent.db.create_step(task.task_id, StepTypes.PLAN)


async def step_handler(step: Step):
    task = await Agent.db.get_task(step.task_id)
    if step.name == StepTypes.PLAN:
        return await _generate_shared_deps(step)
    elif step.name == StepTypes.SPECIFY_FILE_PATHS:
        return await _generate_file_paths(task, step)
    else:
        return await _generate_code(task, step)


Agent.setup_agent(task_handler, step_handler).start()
