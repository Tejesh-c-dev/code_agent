# dev_assistant

dev_assistant is a Python project generator that turns a natural language prompt into a multi-file codebase.

It supports:

- A CLI workflow for local generation
- A FastAPI web app with streaming output (SSE)
- Human-in-the-loop review checkpoints (plan and per-file)
- Optional execution + syntax healing loop
- API key auth, daily rate limits, and usage tracking
- Model routing and fallback across OpenRouter free models
- A training pipeline to collect examples and fine-tune models

## Table of contents

- [What this repository contains](#what-this-repository-contains)
- [How generation works](#how-generation-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick start (CLI)](#quick-start-cli)
- [Run the web app](#run-the-web-app)
- [API usage](#api-usage)
- [Sandbox execution](#sandbox-execution)
- [Training and fine-tuning](#training-and-fine-tuning)
- [Project structure](#project-structure)
- [Common commands](#common-commands)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [License](#license)

## What this repository contains

- `main.py`: top-level CLI entrypoint and `--web` launcher
- `dev_assistant/main.py`: orchestration pipeline (plan -> file list -> generate -> heal)
- `dev_assistant/prompts.py`: prompt templates + OpenRouter calls
- `dev_assistant/openrouter_client.py`: model request + fallback logic
- `dev_assistant/web_server.py`: FastAPI app + web UI + SSE stream
- `dev_assistant/auth/*`: API key auth and rate limiting
- `dev_assistant/db/database.py`: async SQLAlchemy models and DB bootstrap
- `dev_assistant/executor.py`: file execution in Docker or subprocess
- `dev_assistant/healing_loop.py` and `dev_assistant/healer.py`: syntax-heal retries
- `dev_assistant/training/*`: training data, dataset build, fine-tune manager

For architecture details, see `SYSTEM_ARCHITECTURE.md`.

## How generation works

1. Plan step
	 - The model writes a markdown plan and expected file structure.
	 - Output is saved as `shared_deps.md`.
2. File discovery step
	 - File paths are extracted from the plan.
3. Per-file generation step
	 - Each target file is generated independently.
4. Optional review step
	 - CLI: interactive approve/edit/skip.
	 - Web: review events and review API.
5. Optional execution/healing step
	 - Generated files can be executed.
	 - Syntax-like failures trigger healer retries.

## Prerequisites

- Python 3.10+
- Optional but recommended: Docker (for safer execution)
- OpenRouter API key

## Installation

### 1) Clone and enter the repo

```powershell
git clone <your-fork-or-url>
cd dev
```

### 2) Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the repository root.

```env
OPENROUTER_API_KEY=your_openrouter_key

# Optional
FINETUNED_MODEL=
DATABASE_URL=sqlite+aiosqlite:///./dev_assistant.db

# Optional sandbox overrides
SANDBOX_MODE=auto
SANDBOX_TIMEOUT=30
SANDBOX_MEMORY=256m
SANDBOX_NETWORK=false
```

### Environment variables

- `OPENROUTER_API_KEY` (required): used by all generation calls.
- `FINETUNED_MODEL` (optional): preferred codegen model if configured.
- `DATABASE_URL` (optional): defaults to SQLite file `dev_assistant.db`.
- `SANDBOX_MODE` (optional): `docker`, `subprocess`, or `auto`.
- `SANDBOX_TIMEOUT` (optional): execution timeout in seconds.
- `SANDBOX_MEMORY` (optional): Docker memory limit.
- `SANDBOX_NETWORK` (optional): `true` enables network for sandbox runs.

## Quick start (CLI)

### Basic usage

```powershell
python main.py --prompt "Build a responsive todo app" --generate_folder_path output
```

### Important CLI flags

```text
--prompt <text or .md path>
--model <id>                 Global model override for all steps
--plan-model <id>            Plan step model override
--filepath-model <id>        File-path step model override
--codegen-model <id>         Code generation model override
--list-models                List OpenRouter free models and exit
--generate_folder_path <dir> Output directory (default: generated)
--review {none,plan,files,all}
--heal <true|false>
--max-heal-attempts <int>
--no-execute                 Generate only, skip execution/healing
--sandbox {docker,subprocess,auto}
--sandbox-network            Allow network in sandbox execution
--debug <true|false>
```

### Example: review files, do not execute

```powershell
python main.py --prompt "Build a markdown previewer" --review files --no-execute --generate_folder_path output
```

### Example: use per-step models

```powershell
python main.py --prompt "Build a timer web app" --plan-model qwen/qwen3.6-plus:free --filepath-model qwen/qwen3.6-plus:free --codegen-model qwen/qwen3.6-plus:free
```

## Run the web app

### Start server

```powershell
python main.py --web --host 127.0.0.1 --port 8000
```

Open:

- `http://127.0.0.1:8000`

### Web flow overview

1. Register or log in.
2. Receive a generated API key (prefix `dask_`).
3. Start generation from UI (streamed SSE events).
4. Review plan, then review each generated file.
5. Files are written to the selected `output_dir`.

## API usage

The web server exposes both UI and API endpoints.

### Health check

```bash
curl http://127.0.0.1:8000/health
```

### Register

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
	-H "Content-Type: application/json" \
	-d '{"email":"you@example.com","password":"strong-password"}'
```

### Login

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
	-H "Content-Type: application/json" \
	-d '{"email":"you@example.com","password":"strong-password"}'
```

### Authenticated request pattern

Use Bearer auth:

```text
Authorization: Bearer dask_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### List models

```bash
curl http://127.0.0.1:8000/models \
	-H "Authorization: Bearer <API_KEY>"
```

### Start generation (POST, SSE)

```bash
curl -N -X POST http://127.0.0.1:8000/generate \
	-H "Authorization: Bearer <API_KEY>" \
	-H "Content-Type: application/json" \
	-d '{"prompt":"Build a simple notes app","model":null,"output_dir":"./output"}'
```

### Start generation (GET, SSE)

```bash
curl -N "http://127.0.0.1:8000/generate?prompt=Build%20a%20simple%20notes%20app&output_dir=./output" \
	-H "Authorization: Bearer <API_KEY>"
```

### Reviews

- List pending reviews: `GET /reviews/pending`
- Submit review: `POST /review/{review_id}`

Example submit payload:

```json
{
	"action": "approve",
	"edited_content": null,
	"comment": "Looks good"
}
```

### Other endpoints

- `GET /sandbox/status`
- `GET /usage`
- `POST /auth/api-keys`
- `GET /auth/api-keys`
- `DELETE /auth/api-keys/{key_id}`

## Sandbox execution

Sandbox mode controls whether generated files are executed in Docker or via local subprocess.

### Recommended setup

Pull runtime images:

```powershell
docker pull python:3.11-slim
docker pull node:18-slim
docker pull bash:5.2
```

Optional custom image build:

```powershell
docker build -t dev-assistant-python-sandbox -f dev_assistant/sandbox/Dockerfile .
```

### Modes

- `docker`: safest option when Docker is available
- `subprocess`: local execution (less safe)
- `auto`: prefer Docker, fallback to subprocess

## Training and fine-tuning

Training utilities are available through:

```powershell
python -m dev_assistant.training.train <command>
```

### Commands

- `collect`: gather historical examples from database
- `build --min-quality 0.6`: generate `train.jsonl` and `val.jsonl`
- `finetune`: upload datasets and start fine-tune job
- `status --job-id <id>`: check fine-tune job status
- `evaluate`: run sample evaluation of model selection

### Typical training flow

```powershell
python -m dev_assistant.training.train collect
python -m dev_assistant.training.train build --min-quality 0.6
python -m dev_assistant.training.train finetune
```

## Project structure

```text
dev_assistant/
	auth/        # API key auth + rate limiting
	db/          # Async SQLAlchemy models and sessions
	hitl/        # Human-in-the-loop review manager
	sandbox/     # Docker execution and sandbox settings
	training/    # Data collection and fine-tune workflow
	api.py       # Agent Protocol entrypoint
	executor.py  # Runtime execution for generated code
	healer.py    # Syntax repair generation
	main.py      # Core orchestration pipeline
	prompts.py   # Model prompt orchestration
	web_server.py# FastAPI server + in-browser UI
main.py        # CLI + web launcher
```

## Common commands

Install and run CLI:

```powershell
python -m pip install -r requirements.txt
python main.py --prompt "Build a weather dashboard"
```

List available free models:

```powershell
python main.py --list-models
```

Run with web UI:

```powershell
python main.py --web
```

Build/publish package helpers:

```powershell
make build
make publish
```

## Troubleshooting

### OPENROUTER_API_KEY error at startup

Cause: missing environment variable.

Fix: add `OPENROUTER_API_KEY` to `.env` and restart.

### Port already in use

Cause: selected host/port is occupied.

Fix: run web server on another port:

```powershell
python main.py --web --port 8001
```

### Docker unavailable warning

Cause: Docker is not installed/running.

Behavior: runtime falls back to subprocess mode.

Fix: install/start Docker Desktop, or intentionally use `--sandbox subprocess`.

### Daily limit reached (HTTP 429)

Cause: exceeded plan request quota.

Fix: wait for reset or change user plan in storage.

### Model unavailable/rate-limited

Behavior: client attempts configured fallback chain for the current step.

Fix: run `--list-models` and select a currently available free model.

## Security notes

- API keys are generated with `dask_` prefix and stored as hashed values.
- Use Docker mode for code execution whenever possible.
- Path traversal protections reject unsafe output/execution paths.
- Subprocess mode executes code on host and should be treated as unsafe.

## License

MIT. See `LICENSE`.

