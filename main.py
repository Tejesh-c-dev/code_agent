# Quick note: one-line comment added as requested.
"""CLI entrypoint updated for model routing, healing, and web server launch."""

import argparse
import json
import sys

import requests
import uvicorn

from dev_assistant.executor import SandboxMode
from dev_assistant.model_config import get_model
from dev_assistant.sandbox.docker_executor import DockerSandbox
from dev_assistant.sandbox.sandbox_config import SANDBOX_SETTINGS


def _print_model_summary(plan_model: str, filepath_model: str, codegen_model: str) -> None:
    print("Model summary:")
    print(f"  plan: {plan_model}")
    print(f"  file paths: {filepath_model}")
    print(f"  code generation: {codegen_model}")


def _list_models() -> None:
    response = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    response.raise_for_status()
    for item in response.json().get("data", []):
        model_id = item.get("id") or ""
        if not str(model_id).lower().endswith(":free"):
            continue
        name = item.get("name") or model_id
        pricing = item.get("pricing", {})
        print(f"{model_id} | {name} | {json.dumps(pricing, sort_keys=True)}")


def _str_to_bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("Expected a boolean value")


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str, help="Prompt for the app to be created.")
    parser.add_argument("--model", type=str, default=None, help="Global override model for all steps.")
    parser.add_argument("--plan-model", type=str, default=None, help="Override model for planning only.")
    parser.add_argument("--filepath-model", type=str, default=None, help="Override model for file path extraction only.")
    parser.add_argument("--codegen-model", type=str, default=None, help="Override model for code generation only.")
    parser.add_argument("--list-models", action="store_true", help="List available OpenRouter models and exit.")
    parser.add_argument("--generate_folder_path", type=str, default="generated", help="Path of the folder for generated code.")
    parser.add_argument("--debug", type=bool, default=False, help="Enable or disable debug mode.")
    parser.add_argument("--heal", type=_str_to_bool, nargs="?", const=True, default=True, help="Enable or disable the healing loop.")
    parser.add_argument("--max-heal-attempts", type=int, default=3, help="Maximum syntax-healing retries per file.")
    parser.add_argument("--no-execute", action="store_true", help="Skip execution and healing, generate only.")
    parser.add_argument(
        "--review",
        type=str,
        choices=["none", "plan", "files", "all"],
        default="none",
        help="Human review mode: none, plan, files, or all.",
    )
    parser.add_argument(
        "--sandbox",
        type=str,
        choices=[mode.value for mode in SandboxMode],
        default="auto",
        help="Sandbox execution backend: docker, subprocess, or auto.",
    )
    parser.add_argument(
        "--sandbox-network",
        action="store_true",
        help="Allow network access for sandboxed execution.",
    )
    parser.add_argument("--web", action="store_true", help="Start the FastAPI web UI instead of the CLI pipeline.")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host for the FastAPI server.")
    parser.add_argument("--port", type=int, default=8000, help="Port for the FastAPI server.")
    return parser


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

    parser = _build_parser()

    if len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        prompt = sys.argv[1]
        args = argparse.Namespace(
            prompt=None,
            model=None,
            plan_model=None,
            filepath_model=None,
            codegen_model=None,
            list_models=False,
            generate_folder_path="generated",
            debug=False,
            heal=True,
            max_heal_attempts=3,
            no_execute=False,
            review="none",
            sandbox="auto",
            sandbox_network=False,
            web=False,
            host="127.0.0.1",
            port=8000,
        )
    else:
        args = parser.parse_args()
        if args.prompt:
            prompt = args.prompt

    if args.list_models:
        _list_models()
        raise SystemExit(0)

    selected_sandbox_mode = SandboxMode(args.sandbox)
    docker_available = DockerSandbox.is_available()
    if selected_sandbox_mode == SandboxMode.DOCKER and docker_available:
        print("Sandbox: Docker ✅")
    elif selected_sandbox_mode == SandboxMode.DOCKER and not docker_available:
        print("Sandbox: subprocess ⚠️ (unsafe)")
    elif selected_sandbox_mode == SandboxMode.SUBPROCESS:
        print("Sandbox: subprocess ⚠️ (unsafe)")
    elif selected_sandbox_mode == SandboxMode.AUTO:
        if docker_available:
            print("Sandbox: Docker ✅")
        else:
            print("Sandbox: subprocess ⚠️ (unsafe)")

    if args.web:
        from dev_assistant.web_server import app

        SANDBOX_SETTINGS["mode"] = args.sandbox
        SANDBOX_SETTINGS["network_disabled"] = not args.sandbox_network

        print(f"dev_assistant UI running at http://{args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port)
        raise SystemExit(0)

    plan_model = get_model("plan", args.plan_model or args.model)
    filepath_model = get_model("specify_file_paths", args.filepath_model or args.model)
    codegen_model = get_model("generate_code", args.codegen_model or args.model)

    if len(prompt) < 100 and prompt.endswith(".md"):
        with open(prompt, "r") as promptfile:
            prompt = promptfile.read()

    _print_model_summary(plan_model, filepath_model, codegen_model)
    print(prompt)

    from dev_assistant.main import main as run_pipeline

    try:
        run_pipeline(
            prompt=prompt,
            generate_folder_path=args.generate_folder_path,
            debug=args.debug,
            model=args.model,
            plan_model=plan_model,
            filepath_model=filepath_model,
            codegen_model=codegen_model,
            heal=args.heal and not args.no_execute,
            max_heal_attempts=args.max_heal_attempts,
            execute=not args.no_execute,
            review_mode=args.review,
            sandbox_mode=selected_sandbox_mode,
            sandbox_network=args.sandbox_network,
        )
    except Exception as exc:
        print("Generation failed:")
        print(f"  {exc}")
        print("Tips:")
        print("  1. Run --list-models and pick a model ID with low or zero pricing.")
        print("  2. Re-run with --model <id> or per-step model flags.")
        raise SystemExit(1)
