# Quick note: one-line comment added as requested.
"""Pipeline orchestration updated to support per-step model overrides and healing."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal

from dev_assistant.executor import SandboxMode
from dev_assistant.model_config import get_model
from dev_assistant.prompts import generate_code, plan, specify_file_paths
from dev_assistant.healing_loop import generate_and_heal
from dev_assistant.utils import generate_folder, write_file


defaultmodel = None
ReviewMode = Literal["none", "plan", "files", "all"]


def normalize_generated_path(path: str) -> str:
    cleaned = path.strip().strip("`\"'.,:;()[]{}")
    cleaned = cleaned.replace("\\", "/")
    cleaned = cleaned.lstrip("*-_#>")
    cleaned = re.sub(r"[^A-Za-z0-9_./-]", "", cleaned)
    cleaned = re.sub(r"/+", "/", cleaned)
    return cleaned


def _open_in_editor(initial_content: str, suffix: str = ".txt") -> str:
    """Open text in $EDITOR, fallback to nano, and return updated content."""

    with tempfile.NamedTemporaryFile("w+", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(initial_content)
        tmp.flush()
        temp_path = tmp.name

    editor_cmd = os.environ.get("EDITOR")
    if editor_cmd:
        cmd = [editor_cmd, temp_path]
    elif shutil.which("nano"):
        cmd = ["nano", temp_path]
    elif os.name == "nt":
        cmd = ["notepad", temp_path]
    else:
        cmd = ["vi", temp_path]

    try:
        subprocess.run(cmd, check=False)
        with open(temp_path, "r", encoding="utf-8") as updated_file:
            return updated_file.read()
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _review_plan_interactively(plan_text: str) -> tuple[str, str]:
    """Prompt the user to approve, reject, or edit the generated plan."""

    print("\n--- Plan Review ---")
    print(plan_text)
    print("\nApprove? [y/n/edit]")
    while True:
        choice = input("> ").strip().lower()
        if choice in {"y", "yes"}:
            return "approve", plan_text
        if choice in {"n", "no"}:
            return "reject_all", plan_text
        if choice in {"edit", "e"}:
            edited = _open_in_editor(plan_text, suffix=".md")
            return "edit", edited
        print("Enter y, n, or edit.")


def _review_file_interactively(file_path: str, code_text: str) -> tuple[str, str]:
    """Prompt the user to write, skip, or edit generated file content."""

    print(f"\n--- File Review: {file_path} ---")
    print(code_text)
    print("\nWrite? [y/n/edit]")
    while True:
        choice = input("> ").strip().lower()
        if choice in {"y", "yes"}:
            return "approve", code_text
        if choice in {"n", "no"}:
            return "skip", code_text
        if choice in {"edit", "e"}:
            suffix = Path(file_path).suffix or ".txt"
            edited = _open_in_editor(code_text, suffix=suffix)
            return "edit", edited
        print("Enter y, n, or edit.")


def main(
    prompt,
    generate_folder_path="generated",
    debug=False,
    model: str = defaultmodel,
    plan_model: str = None,
    filepath_model: str = None,
    codegen_model: str = None,
    heal: bool = True,
    max_heal_attempts: int = 3,
    execute: bool = True,
    sandbox_mode: SandboxMode | str = SandboxMode.AUTO,
    sandbox_network: bool = False,
    review_mode: ReviewMode = "none",
):
    """Generate a project from a prompt using the configured model routing."""

    plan_model = get_model("plan", plan_model or model)
    filepath_model = get_model("specify_file_paths", filepath_model or model)
    codegen_model = get_model("generate_code", codegen_model or model)

    # create generateFolder folder if doesnt exist
    generate_folder(generate_folder_path)

    # plan shared_deps
    if debug:
        print("--------shared_deps---------")
    with open(f"{generate_folder_path}/shared_deps.md", "wb") as f:
        start_time = time.time()

        def stream_handler(chunk):
            f.write(chunk)
            if debug:
                end_time = time.time()
                sys.stdout.write(
                    "\r \033[93mChars streamed\033[0m: {}. \033[93mChars per second\033[0m: {:.2f}".format(
                        stream_handler.count,
                        stream_handler.count / (end_time - start_time),
                    )
                )
                sys.stdout.flush()
                stream_handler.count += len(chunk)

        stream_handler.count = 0
        stream_handler.onComplete = lambda x: sys.stdout.write("\033[0m\n")

        shared_deps = plan(prompt, stream_handler, model=plan_model)
    if debug:
        print(shared_deps)
    write_file(f"{generate_folder_path}/shared_deps.md", shared_deps)
    if debug:
        print("--------shared_deps---------")

    # specify file_paths
    if debug:
        print("--------specify_filePaths---------")
    file_paths = specify_file_paths(prompt, shared_deps, model=filepath_model)
    if debug:
        print(file_paths)
        print("--------file_paths---------")

    healing_results = []

    if review_mode in {"plan", "all"}:
        action, reviewed_plan = _review_plan_interactively(shared_deps)
        if action == "reject_all":
            print("Generation cancelled during plan review.")
            return
        if action == "edit":
            shared_deps = reviewed_plan
            write_file(f"{generate_folder_path}/shared_deps.md", shared_deps)

    if review_mode in {"files", "all"} and execute:
        print("Review mode is enabled. Execution/healing is skipped for edited files.")

    # loop through file_paths array and generate code for each file
    for file_path in file_paths:
        file_path = normalize_generated_path(file_path)
        if not file_path:
            continue
        if ".." in Path(file_path).parts:
            if debug:
                print(f"Skipping unsafe path: {file_path}")
            continue
        output_path = f"{generate_folder_path}/{file_path}"
        if debug:
            print(f"--------generate_code: {output_path} ---------")

        if review_mode in {"files", "all"}:
            generated_code = asyncio.run(
                generate_code(
                    prompt,
                    shared_deps,
                    file_path,
                    model=codegen_model,
                )
            )
            file_action, reviewed_code = _review_file_interactively(file_path, generated_code)
            if file_action == "skip":
                continue
            write_file(output_path, reviewed_code)
            continue

        result = asyncio.run(
            generate_and_heal(
                file_path=file_path,
                shared_dependencies=shared_deps,
                prompt=prompt,
                output_dir=generate_folder_path,
                generate_model=codegen_model,
                heal_model=codegen_model,
                max_heal_attempts=max_heal_attempts,
                heal=heal,
                execute=execute,
                debug=debug,
                sandbox_mode=sandbox_mode,
                sandbox_network=sandbox_network,
            )
        )
        healing_results.append(result)

    if healing_results:
        print()
        print("File                  | Attempts | Status")
        print("----------------------|----------|--------")
        for result in healing_results:
            if result.execution_skipped:
                status = "⏭️ Skipped"
            elif result.succeeded and result.attempts == 0:
                status = "✅ Clean"
            elif result.succeeded:
                status = "✅ Healed"
            else:
                status = "❌ Failed"
            print(f"{result.file_path:<22} | {result.attempts:<8} | {status}")

    print("--------dev assistant done!---------")


# for local testing
# python main.py --prompt "a simple JavaScript/HTML/CSS/Canvas app that is a one player game of PONG..." --generate_folder_path "generated" --debug True

if __name__ == "__main__":
    prompt = """
  a simple JavaScript/HTML/CSS/Canvas app that is a one player game of PONG.
  The left paddle is controlled by the player, following where the mouse goes.
  The right paddle is controlled by a simple AI algorithm, which slowly moves the paddle toward the ball at every frame, with some probability of error.
  Make the canvas a 400 x 400 black square and center it in the app.
  Make the paddles 100px long, yellow and the ball small and red.
  Make sure to render the paddles and name them so they can controlled in javascript.
  Implement the collision detection and scoring as well.
  Every time the ball bouncess off a paddle, the ball should move faster.
  It is meant to run in Chrome browser, so dont use anything that is not supported by Chrome, and don't use the import and export keywords.
  """
    import argparse

    if len(sys.argv) == 2:
        prompt = sys.argv[1]
        args = type(
            "Args",
            (),
            {
                "generate_folder_path": "generated",
                "debug": False,
            },
        )()
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--prompt", type=str, required=True, help="Prompt for the app to be created.")
        parser.add_argument("--generate_folder_path", type=str, default="generated", help="Path of the folder for generated code.")
        parser.add_argument("--debug", type=bool, default=False, help="Enable or disable debug mode.")
        args = parser.parse_args()
        if args.prompt:
            prompt = args.prompt

    print(prompt)

    main(prompt=prompt, generate_folder_path=args.generate_folder_path, debug=args.debug)
