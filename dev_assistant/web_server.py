# Quick note: one-line comment added as requested.
"""FastAPI web UI for streaming dev_assistant generations in the browser."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dev_assistant.auth.api_key_auth import create_api_key_for_user, get_current_user, hash_password, verify_password
from dev_assistant.auth.rate_limiter import PLAN_LIMITS, rate_limit_check
from dev_assistant.db.database import AsyncSessionLocal, GenerationJob, User, fetch_api_key_by_hash, fetch_user_by_email, init_db
from dev_assistant.hitl.review_manager import ReviewManager
from dev_assistant.model_config import get_model
from dev_assistant.prompts import generate_code, plan, specify_file_paths
from dev_assistant.sandbox.docker_executor import DockerSandbox, LANGUAGE_IMAGES
from dev_assistant.sandbox.sandbox_config import SANDBOX_SETTINGS
from dev_assistant.utils import write_file


APP_VERSION = "1.0.0"
MODEL_CACHE_TTL_SECONDS = 600
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


app = FastAPI(title="dev_assistant")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerationRequest(BaseModel):
    """Incoming generation request for the streaming endpoints."""

    prompt: str = Field(..., min_length=1)
    model: Optional[str] = None
    output_dir: str = Field("./output")

class AuthPayload(BaseModel):
    email: str
    password: str


class ApiKeyPayload(BaseModel):
    name: str = "default"


class UsageLimitResponse(BaseModel):
    limit: int
    remaining: int
    reset: int


class ReviewSubmission(BaseModel):
    """Incoming review action for a pending request."""

    action: str = Field(..., regex="^(approve|edit|skip|reject_all)$")
    edited_content: Optional[str] = None
    comment: Optional[str] = None


_MODEL_CACHE: Dict[str, Any] = {"timestamp": 0.0, "data": []}


@app.on_event("startup")
async def _init_review_manager() -> None:
    """Initialize HITL manager for review pause/resume checkpoints."""

    await init_db()
    app.state.review_manager = ReviewManager()


@app.middleware("http")
async def _add_rate_limit_headers(request: Request, call_next):
    response = await call_next(request)
    rate_limit_state = getattr(request.state, "rate_limit", None) or {
        "limit": PLAN_LIMITS["free"]["requests_per_day"],
        "remaining": PLAN_LIMITS["free"]["requests_per_day"],
      "reset": int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
    }
    response.headers["X-RateLimit-Limit"] = str(rate_limit_state["limit"])
    response.headers["X-RateLimit-Remaining"] = str(rate_limit_state["remaining"])
    response.headers["X-RateLimit-Reset"] = str(rate_limit_state["reset"])
    return response


def _sse_event(payload: dict) -> str:
    """Serialize one SSE data event as a single line."""

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sanitize_output_dir(output_dir: str) -> Path:
    """Validate the output directory and reject traversal segments."""

    candidate = Path((output_dir or "./output").strip() or "./output").expanduser()
    if any(part == ".." for part in candidate.parts):
        raise HTTPException(status_code=400, detail="Output directory cannot contain '..' segments")
    return candidate.resolve(strict=False)


def _normalized_model_for_step(step: str, model: Optional[str]) -> str:
    """Return the override model for a step or the configured default."""

    return get_model(step, model) if model else get_model(step)


def _fetch_models_from_openrouter() -> List[dict]:
    """Fetch and normalize the OpenRouter model list."""

    response = requests.get(OPENROUTER_MODELS_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    models = payload.get("data", payload if isinstance(payload, list) else [])

    normalized: List[dict] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("name") or ""
        pricing = item.get("pricing") or {}
        if not str(model_id).lower().endswith(":free"):
            continue
        normalized.append(
            {
                "id": model_id,
                "name": item.get("name") or model_id or "Unknown model",
                "pricing": pricing,
                "context_length": item.get("context_length"),
                "architecture": item.get("architecture"),
            }
        )
    return normalized


def _get_cached_models() -> List[dict]:
    """Return cached models, refreshing every ten minutes."""

    import time

    now = time.time()
    cached = _MODEL_CACHE["data"]
    if cached and now - float(_MODEL_CACHE["timestamp"]) < MODEL_CACHE_TTL_SECONDS:
        return cached

    try:
        models = _fetch_models_from_openrouter()
        _MODEL_CACHE["timestamp"] = now
        _MODEL_CACHE["data"] = models
        return models
    except Exception:
        if cached:
            return cached
        raise


def _sandbox_status_payload() -> dict:
    docker_available = DockerSandbox.is_available()
    mode = str(SANDBOX_SETTINGS.get("mode", "auto")).lower()

    images_pulled: List[str] = []
    if docker_available:
        import subprocess

        for image in sorted(set(LANGUAGE_IMAGES.values())):
            check = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode == 0:
                images_pulled.append(image)

    return {
        "docker_available": docker_available,
        "mode": mode,
        "images_pulled": images_pulled,
    }


def _start_plan_stream(prompt: str, model: str) -> tuple[asyncio.Task[tuple[str, Any]], asyncio.Queue[str]]:
  """Start the planning step in a worker thread and return a task plus queue."""

  queue: asyncio.Queue[str] = asyncio.Queue()
  loop = asyncio.get_running_loop()

  def _on_chunk(chunk: bytes) -> None:
    loop.call_soon_threadsafe(queue.put_nowait, chunk.decode("utf-8", errors="replace"))

  def _run_plan() -> tuple[str, Any]:
    return plan(prompt, stream_handler=_on_chunk, model=model, return_usage=True)

  task = asyncio.create_task(asyncio.to_thread(_run_plan))
  return task, queue


def _start_generate_code_stream(
  prompt: str,
  shared_dependencies: str,
  current_file: str,
  model: str,
) -> tuple[asyncio.Task[tuple[str, Any]], asyncio.Queue[str]]:
  """Start code generation in a worker thread and return a task plus queue."""

  queue: asyncio.Queue[str] = asyncio.Queue()
  loop = asyncio.get_running_loop()

  def _on_chunk(chunk: bytes) -> None:
    loop.call_soon_threadsafe(queue.put_nowait, chunk.decode("utf-8", errors="replace"))

  def _run_code() -> tuple[str, Any]:
    return asyncio.run(
      generate_code(
        prompt,
        shared_dependencies,
        current_file,
        stream_handler=_on_chunk,
        model=model,
        return_usage=True,
      )
    )

  task = asyncio.create_task(asyncio.to_thread(_run_code))
  return task, queue


async def _generation_events(request: GenerationRequest, user: User) -> AsyncGenerator[str, None]:
  """Stream the generation pipeline as SSE events."""

  async def _start_review(
    review_type: str,
    content: str,
    file_path: Optional[str],
  ):
    review_manager: ReviewManager = app.state.review_manager
    existing_ids = set(review_manager.pending_reviews.keys())
    review_task = asyncio.create_task(review_manager.request_review(review_type, content, file_path=file_path))

    review_request = None
    while review_request is None:
      for pending in review_manager.get_pending():
        if pending.review_id not in existing_ids:
          review_request = pending
          break
      if review_request is None:
        if review_task.done():
          break
        await asyncio.sleep(0.02)

    return review_request, review_task

  try:
    if not request.prompt.strip():
      raise HTTPException(status_code=400, detail="Prompt is required")

    output_dir = _sanitize_output_dir(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with AsyncSessionLocal() as db:
      job = GenerationJob(
        user_id=user.id,
        prompt=request.prompt,
        model=request.model or "free-default",
        status="running",
        files_generated=0,
        tokens_used=0,
        cost_usd=0.0,
        output_dir=str(output_dir),
        heal_attempts=0,
      )
      db.add(job)
      await db.commit()
      await db.refresh(job)

      async def _update_job(status_value: str, files_count: int, tokens_used: int, cost_usd: float) -> None:
        job.status = status_value
        job.files_generated = files_count
        job.tokens_used = tokens_used
        job.cost_usd = cost_usd
        if status_value in {"completed", "cancelled", "failed"}:
          from datetime import datetime, timezone

          job.completed_at = datetime.now(timezone.utc)
        await db.commit()

      def _usage_tokens(usage: Any) -> int:
        if isinstance(usage, dict):
          return int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        return int(getattr(usage, "total_tokens", 0) or 0)

      def _estimate_cost(model_name: str, total_tokens: int) -> float:
        if total_tokens <= 0:
          return 0.0
        pricing_map = {item.get("id"): item.get("pricing", {}) for item in _get_cached_models()}
        pricing = pricing_map.get(model_name, {}) or {}
        try:
          prompt_price = float(pricing.get("prompt", 0) or 0)
          completion_price = float(pricing.get("completion", 0) or 0)
        except Exception:
          prompt_price = completion_price = 0.0
        unit_price = (prompt_price + completion_price) / 2 if (prompt_price or completion_price) else 0.0
        return unit_price * total_tokens

      requested_model = request.model or None
      plan_model = _normalized_model_for_step("plan", requested_model)
      filepath_model = _normalized_model_for_step("specify_file_paths", requested_model)
      codegen_model = _normalized_model_for_step("generate_code", requested_model)
      total_tokens = 0
      total_cost = 0.0

      yield _sse_event({"type": "status", "message": "Starting plan..."})
      plan_task, plan_queue = _start_plan_stream(request.prompt, plan_model)
      while not plan_task.done() or not plan_queue.empty():
        try:
          chunk = await asyncio.wait_for(plan_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
          continue
        yield _sse_event({"type": "plan_chunk", "content": chunk})
      plan_text, plan_usage = await plan_task
      total_tokens += _usage_tokens(plan_usage)
      total_cost += _estimate_cost(plan_model, _usage_tokens(plan_usage))

      plan_request, plan_task = await _start_review("plan", plan_text, file_path=None)
      if plan_request is None:
        raise RuntimeError("Plan review request was not created")

      yield _sse_event(
        {
          "type": "review_required",
          "review_id": plan_request.review_id,
          "review_type": "plan",
          "file_path": None,
          "content": plan_text,
          "message": "Review the plan before continuing",
        }
      )
      plan_result = await plan_task
      yield _sse_event(
        {
          "type": "review_resolved",
          "review_id": plan_result.review_id,
          "review_type": "plan",
          "action": plan_result.action,
          "comment": plan_result.comment,
        }
      )

      if plan_result.action == "edit" and plan_result.edited_content:
        plan_text = plan_result.edited_content
        yield _sse_event({"type": "plan_updated", "message": "Using reviewed plan edits"})
      if plan_result.action == "reject_all":
        await _update_job("cancelled", 0, total_tokens, total_cost)
        yield _sse_event({"type": "cancelled", "message": "Generation cancelled during plan review"})
        return

      shared_deps_path = output_dir / "shared_deps.md"
      write_file(str(shared_deps_path), plan_text)

      yield _sse_event({"type": "status", "message": "Discovering files..."})
      files, file_usage = await asyncio.to_thread(
        specify_file_paths,
        request.prompt,
        plan_text,
        filepath_model,
        True,
      )
      total_tokens += _usage_tokens(file_usage)
      total_cost += _estimate_cost(filepath_model, _usage_tokens(file_usage))
      yield _sse_event({"type": "file_list", "files": files})

      total_files = len(files)
      completed_files = 0

      for current_file in files:
        current_path = Path(current_file)
        if any(part == ".." for part in current_path.parts):
          raise HTTPException(status_code=400, detail=f"Unsafe file path: {current_file}")

        yield _sse_event({"type": "status", "message": f"Generating {current_file}..."})
        yield _sse_event({"type": "file_start", "file": current_file})

        code_task, code_queue = _start_generate_code_stream(
          request.prompt,
          plan_text,
          current_file,
          codegen_model,
        )
        while not code_task.done() or not code_queue.empty():
          try:
            chunk = await asyncio.wait_for(code_queue.get(), timeout=0.1)
          except asyncio.TimeoutError:
            continue
          yield _sse_event({"type": "code_chunk", "file": current_file, "content": chunk})

        code_text, code_usage = await code_task
        total_tokens += _usage_tokens(code_usage)
        total_cost += _estimate_cost(codegen_model, _usage_tokens(code_usage))
        file_request, file_task = await _start_review("file", code_text, file_path=current_file)
        if file_request is None:
          raise RuntimeError(f"File review resolution missing for {current_file}")

        yield _sse_event(
          {
            "type": "review_required",
            "review_id": file_request.review_id,
            "review_type": "file",
            "file_path": current_file,
            "content": code_text,
            "message": f"Review {current_file} before writing to disk",
          }
        )
        file_result = await file_task
        yield _sse_event(
          {
            "type": "review_resolved",
            "review_id": file_result.review_id,
            "review_type": "file",
            "file_path": current_file,
            "action": file_result.action,
            "comment": file_result.comment,
          }
        )

        if file_result.action == "reject_all":
          await _update_job("cancelled", completed_files, total_tokens, total_cost)
          yield _sse_event({"type": "cancelled", "message": "Generation cancelled by reviewer"})
          return
        if file_result.action == "skip":
          yield _sse_event({"type": "file_skipped", "file": current_file})
          continue
        if file_result.action == "edit" and file_result.edited_content is not None:
          code_text = file_result.edited_content

        output_file = output_dir / current_file
        write_file(str(output_file), code_text)

        completed_files += 1
        yield _sse_event({"type": "file_done", "file": current_file})
        yield _sse_event({"type": "status", "message": f"Files: {completed_files} / {total_files} complete"})

      await _update_job("completed", completed_files, total_tokens, total_cost)
      yield _sse_event({"type": "done", "message": "Generation complete", "output_dir": str(output_dir)})
  except HTTPException as exc:
    yield _sse_event({"type": "error", "message": exc.detail})
  except Exception as exc:
    yield _sse_event({"type": "error", "message": str(exc)})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the single-page web UI."""

    return HTMLResponse(_HTML_PAGE)


@app.get("/health")
async def health() -> dict:
    """Return a simple health payload."""

    return {"status": "ok", "version": APP_VERSION}


@app.get("/models")
async def models(current_user: User = Depends(get_current_user)) -> JSONResponse:
    """Return cached OpenRouter models for the UI dropdown."""

    return JSONResponse(_get_cached_models())


@app.get("/sandbox/status")
async def sandbox_status() -> JSONResponse:
    """Return sandbox backend availability and image readiness."""

    return JSONResponse(_sandbox_status_payload())


def _serialize_user(user: User) -> dict[str, Any]:
  return {
    "id": user.id,
    "email": user.email,
    "plan": user.plan,
    "created_at": user.created_at.isoformat() if user.created_at else None,
  }


@app.post("/auth/register")
async def auth_register(payload: AuthPayload) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    existing_user = await fetch_user_by_email(db, payload.email)
    if existing_user is not None:
      raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")
    user = User(email=payload.email, hashed_password=hash_password(payload.password), plan="free")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    api_key, raw_key = await create_api_key_for_user(db, user, name="default")
    return JSONResponse({"user": _serialize_user(user), "api_key": raw_key, "key_id": api_key.id})


@app.post("/auth/login")
async def auth_login(payload: AuthPayload) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    user = await fetch_user_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.hashed_password):
      raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    api_key, raw_key = await create_api_key_for_user(db, user, name="login")
    return JSONResponse({"user": _serialize_user(user), "api_key": raw_key, "key_id": api_key.id})


@app.post("/auth/api-keys")
async def auth_create_api_key(payload: ApiKeyPayload, current_user: User = Depends(get_current_user)) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    result = await db.execute(select(User).where(User.id == current_user.id))
    refreshed_user = result.scalar_one_or_none()
    if refreshed_user is None:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    api_key, raw_key = await create_api_key_for_user(db, refreshed_user, name=payload.name)
    return JSONResponse({"id": api_key.id, "name": api_key.name, "api_key": raw_key})


@app.get("/auth/api-keys")
async def auth_list_api_keys(current_user: User = Depends(get_current_user)) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    result = await db.execute(select(User).options(selectinload(User.api_keys)).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if user is None:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    keys = [
      {
        "id": api_key.id,
        "name": api_key.name,
        "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
        "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
        "is_active": api_key.is_active,
      }
      for api_key in user.api_keys
    ]
    return JSONResponse(keys)


@app.delete("/auth/api-keys/{key_id}")
async def auth_delete_api_key(key_id: str, current_user: User = Depends(get_current_user)) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    result = await db.execute(select(User).options(selectinload(User.api_keys)).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if user is None:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    for api_key in user.api_keys:
      if api_key.id == key_id:
        api_key.is_active = False
        await db.commit()
        return JSONResponse({"status": "ok"})
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")


@app.get("/usage")
async def usage(current_user: User = Depends(get_current_user)) -> JSONResponse:
  async with AsyncSessionLocal() as db:
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if user is None:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    query = await db.execute(select(GenerationJob).where(GenerationJob.user_id == user.id).order_by(GenerationJob.created_at.desc()).limit(5))
    jobs = query.scalars().all()
    limit = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])["requests_per_day"]
    recent_jobs = [
      {
        "id": job.id,
        "prompt_preview": (job.prompt[:120] + "...") if len(job.prompt) > 120 else job.prompt,
        "status": job.status,
        "files": job.files_generated,
        "tokens": job.tokens_used,
        "cost": job.cost_usd,
        "created_at": job.created_at.isoformat() if job.created_at else None,
      }
      for job in jobs
    ]
    reset_at = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    hours_remaining = max(int((reset_at - datetime.now(timezone.utc)).total_seconds() // 3600), 0)
    return JSONResponse(
      {
        "plan": user.plan,
        "api_calls_today": user.api_calls_today,
        "api_calls_total": user.api_calls_total,
        "daily_limit": limit,
        "reset_time": f"resets in {hours_remaining} hours",
        "recent_jobs": recent_jobs,
      }
    )


@app.post("/review/{review_id}")
async def submit_review(review_id: str, payload: ReviewSubmission, current_user: User = Depends(get_current_user)) -> JSONResponse:
    """Resolve a pending review request and resume generation."""

    review_manager: ReviewManager = app.state.review_manager
    try:
        review_manager.submit_review(
            review_id=review_id,
            action=payload.action,
            edited_content=payload.edited_content,
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"status": "ok", "review_id": review_id})


@app.get("/reviews/pending")
async def pending_reviews(current_user: User = Depends(get_current_user)) -> JSONResponse:
    """Return unresolved review requests for UI polling fallback."""

    review_manager: ReviewManager = app.state.review_manager
    return JSONResponse([asdict(item) for item in review_manager.get_pending()])


@app.post("/generate")
async def generate_post(payload: GenerationRequest, current_user: User = Depends(rate_limit_check)) -> StreamingResponse:
    """Start a streamed generation from a JSON body."""

    return StreamingResponse(_generation_events(payload, current_user), media_type="text/event-stream")


@app.get("/generate")
async def generate_get(
    prompt: str = Query(..., min_length=1),
    model: Optional[str] = Query(default=None),
    output_dir: str = Query(default="./output"),
    current_user: User = Depends(rate_limit_check),
) -> StreamingResponse:
    """Start a streamed generation from query parameters for EventSource compatibility."""

    payload = GenerationRequest(prompt=prompt, model=model, output_dir=output_dir)
    return StreamingResponse(_generation_events(payload, current_user), media_type="text/event-stream")


_HTML_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>dev_assistant</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: light;
      --bg: #fafaf7;
      --bg-soft: #f3f4ec;
      --panel: rgba(255, 255, 250, 0.64);
      --panel-strong: rgba(255, 255, 252, 0.84);
      --text: #1f2937;
      --muted: #667085;
      --accent-a: #0f766e;
      --accent-b: #0e7490;
      --accent-c: #f59e0b;
      --accent-cyan: #14b8a6;
      --border-soft: rgba(148, 163, 184, 0.18);
      --success: #0f766e;
      --danger: #be123c;
      --warning: #b45309;
      --shadow-lg: 0 24px 80px rgba(15, 23, 42, 0.12);
      --shadow-md: 0 12px 34px rgba(15, 23, 42, 0.08);
      --radius-xl: 20px;
      --radius-lg: 16px;
      --radius-md: 12px;
      --font-sans: "Inter", ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --space-1: 8px;
      --space-2: 16px;
      --space-3: 24px;
      --space-4: 32px;
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      height: 100%;
    }

    body {
      margin: 0;
      font-family: var(--font-sans);
      background:
        radial-gradient(circle at 8% 0%, rgba(15, 118, 110, 0.12), transparent 32%),
        radial-gradient(circle at 100% 10%, rgba(245, 158, 11, 0.1), transparent 34%),
        radial-gradient(circle at 20% 100%, rgba(14, 116, 144, 0.09), transparent 28%),
        linear-gradient(180deg, #fafaf7 0%, #f4f4ef 100%);
      color: var(--text);
      overflow-x: hidden;
    }

    #cursor-glow {
      position: fixed;
      width: 280px;
      height: 280px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(15, 118, 110, 0.22) 0%, rgba(245, 158, 11, 0.14) 28%, rgba(255,255,255,0) 68%);
      pointer-events: none;
      z-index: 1;
      transform: translate(-50%, -50%);
      transition: transform 0.08s linear;
    }

    .app {
      position: relative;
      z-index: 2;
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: var(--space-2);
      padding: var(--space-2);
    }

    .hidden {
      display: none !important;
    }

    .auth-screen {
      position: fixed;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(15, 23, 42, 0.24);
      backdrop-filter: blur(10px);
      z-index: 1200;
      padding: 24px;
    }

    .auth-card {
      width: min(520px, 100%);
      padding: 28px;
      border-radius: 24px;
      border: 1px solid var(--border-soft);
      background: var(--panel-strong);
      box-shadow: var(--shadow-lg);
      backdrop-filter: blur(16px);
      display: grid;
      gap: 12px;
      animation: floatIn 0.45s ease;
    }

    .auth-card h1 {
      margin: 0;
      font-size: 1.4rem;
      letter-spacing: -0.02em;
    }

    .auth-card p {
      margin: 0 0 8px;
      color: var(--muted);
    }

    .auth-card .button-row {
      grid-template-columns: 1fr 1fr;
    }

    .header {
      position: sticky;
      top: var(--space-2);
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      padding: 14px 16px;
      border-radius: var(--radius-lg);
      background: var(--panel);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-md);
      border: 1px solid var(--border-soft);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
      font-size: 1rem;
    }

    .brand-mark {
      width: 40px;
      height: 40px;
      border-radius: 13px;
      display: grid;
      place-items: center;
      background: linear-gradient(130deg, rgba(15, 118, 110, 0.24), rgba(245, 158, 11, 0.2));
      color: #115e59;
      font-family: var(--font-mono);
      border: 1px solid rgba(13, 148, 136, 0.24);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.25);
    }

    .header-note {
      color: var(--muted);
      font-size: 0.86rem;
      font-weight: 500;
    }

    .header-controls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .header-controls .secondary {
      padding: 8px 12px;
      font-size: 0.84rem;
      border-radius: 999px;
    }

    .shell {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr) minmax(360px, 420px);
      gap: var(--space-2);
      min-width: 0;
      align-items: start;
    }

    .panel {
      border-radius: var(--radius-xl);
      background: var(--panel);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-md);
      border: 1px solid var(--border-soft);
      overflow: hidden;
      animation: fadeUp 0.38s ease;
    }

    .composer {
      padding: var(--space-2);
      display: grid;
      gap: var(--space-2);
      position: sticky;
      top: 88px;
    }

    .usage-panel {
      display: grid;
      gap: 10px;
      padding: 14px;
      border-radius: var(--radius-lg);
      background:
        linear-gradient(130deg, rgba(15, 118, 110, 0.12), rgba(14, 116, 144, 0.09) 42%, rgba(245, 158, 11, 0.12));
      border: 1px solid rgba(13, 148, 136, 0.2);
    }

    .usage-title {
      font-weight: 600;
      color: #1e293b;
      letter-spacing: -0.01em;
    }

    .usage-metric {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.92rem;
    }

    .usage-meter {
      height: 10px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.25);
      overflow: hidden;
    }

    .usage-meter > div {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent-a), var(--accent-b), var(--accent-cyan));
      width: 0%;
      transition: width 0.45s ease;
      box-shadow: 0 0 12px rgba(245, 158, 11, 0.3);
    }

    .usage-warning {
      color: var(--warning);
      font-size: 0.9rem;
    }

    .review-toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #334155;
      font-size: 0.88rem;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.28);
      background: rgba(255, 255, 255, 0.7);
      transition: all 0.2s ease;
    }

    .review-toggle:hover {
      border-color: rgba(15, 118, 110, 0.32);
      box-shadow: 0 8px 24px rgba(13, 148, 136, 0.14);
    }

    .review-toggle input {
      width: 16px;
      height: 16px;
      accent-color: var(--accent-a);
    }

    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      border: 1px solid rgba(15, 118, 110, 0.2);
      background: rgba(255, 255, 255, 0.65);
      color: #334155;
      font-size: 0.82rem;
      padding: 7px 11px;
      font-weight: 600;
    }

    .status-chip.dot::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: currentColor;
    }

    .field-stack {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .field label {
      display: block;
      margin-bottom: 7px;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--muted);
      letter-spacing: 0.01em;
    }

    textarea,
    input,
    select,
    button {
      font: inherit;
    }

    textarea,
    input,
    select {
      width: 100%;
      color: var(--text);
      background: rgba(255, 255, 255, 0.8);
      border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 14px;
      padding: 12px 14px;
      outline: none;
      transition: border-color 0.18s ease, box-shadow 0.18s ease;
    }

    textarea:focus, input:focus, select:focus {
      border-color: rgba(15, 118, 110, 0.48);
      box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.13), 0 0 20px rgba(245, 158, 11, 0.13);
    }

    textarea {
      min-height: 200px;
      resize: vertical;
      line-height: 1.5;
      font-size: 0.95rem;
    }

    .button-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    button {
      border: none;
      border-radius: 13px;
      padding: 11px 13px;
      cursor: pointer;
      font-weight: 600;
      transition: transform 0.16s ease, opacity 0.16s ease, box-shadow 0.16s ease;
    }

    button:hover:not(:disabled) {
      transform: translateY(-1px);
    }

    button:disabled { opacity: 0.55; cursor: not-allowed; }

    .primary {
      background: linear-gradient(110deg, var(--accent-a), var(--accent-b), var(--accent-c));
      color: #ffffff;
      box-shadow: 0 14px 28px rgba(15, 118, 110, 0.26);
    }

    .secondary {
      background: rgba(255, 255, 255, 0.74);
      color: #1e293b;
      border: 1px solid rgba(148, 163, 184, 0.26);
    }

    .plan-panel {
      padding: var(--space-2);
      display: grid;
      gap: var(--space-2);
      min-width: 0;
      align-self: stretch;
    }

    .banner {
      display: none;
      padding: 12px 14px;
      border-radius: var(--radius-md);
      border: 1px solid transparent;
      font-weight: 600;
      font-size: 0.9rem;
    }

    .banner.visible {
      display: block;
      animation: fadeUp 0.25s ease;
    }

    .banner.success {
      background: rgba(16, 185, 129, 0.12);
      border-color: rgba(16, 185, 129, 0.26);
      color: #065f46;
    }

    .banner.error {
      background: rgba(225, 29, 72, 0.12);
      border-color: rgba(225, 29, 72, 0.25);
      color: #9f1239;
    }

    .thinking-card {
      display: grid;
      gap: 10px;
      padding: 14px;
      border-radius: var(--radius-lg);
      background: rgba(255, 255, 255, 0.8);
      border: 1px solid rgba(148, 163, 184, 0.22);
    }

    .heading-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .section-title {
      margin: 0 0 12px;
      font-size: 1.02rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #0f172a;
    }

    .hint {
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .stream-box {
      min-height: 280px;
      max-height: min(58vh, 560px);
      overflow: auto;
      padding: 16px;
      background: rgba(248, 250, 252, 0.78);
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.24);
      white-space: pre-wrap;
      line-height: 1.6;
      color: #0f172a;
      font-family: var(--font-mono);
      font-size: 0.9rem;
    }

    .typing-indicator {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 0.83rem;
      font-weight: 600;
    }

    .typing-indicator span {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: linear-gradient(120deg, var(--accent-a), var(--accent-cyan));
      animation: blink 1.2s infinite ease-in-out;
    }

    .typing-indicator span:nth-child(2) {
      animation-delay: 0.16s;
    }

    .typing-indicator span:nth-child(3) {
      animation-delay: 0.3s;
    }

    .workspace {
      display: grid;
      gap: 0;
      padding: var(--space-2);
      align-self: stretch;
      min-width: 0;
    }

    .tabs {
      display: flex;
      align-items: center;
      gap: 10px;
      padding-bottom: 12px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.2);
      margin-bottom: 12px;
    }

    .tab-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      border-radius: 12px;
      border: 1px solid transparent;
      background: rgba(255, 255, 255, 0.55);
      color: #334155;
      font-weight: 600;
      font-size: 0.9rem;
      transition: all 0.2s ease;
    }

    .tab-btn svg {
      width: 15px;
      height: 15px;
    }

    .tab-btn.active {
      background: linear-gradient(120deg, rgba(15, 118, 110, 0.16), rgba(245, 158, 11, 0.14));
      border-color: rgba(15, 118, 110, 0.32);
      color: #134e4a;
      box-shadow: 0 8px 24px rgba(13, 148, 136, 0.14);
    }

    .tab-panel {
      min-height: 420px;
      max-height: min(66vh, 690px);
      overflow: auto;
      padding-right: 4px;
    }

    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      padding: 8px 11px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.74);
      border: 1px solid rgba(148, 163, 184, 0.24);
      color: #334155;
      font-size: 0.86rem;
      transition: all 0.2s ease;
    }

    .chip:hover {
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.3);
      color: #134e4a;
    }

    .suggestions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }

    .suggestion-chip {
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.24);
      background: rgba(255, 255, 255, 0.8);
      color: #475569;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .suggestion-chip:hover {
      color: #134e4a;
      border-color: rgba(15, 118, 110, 0.3);
      box-shadow: 0 8px 18px rgba(13, 148, 136, 0.14);
    }

    .code-list {
      display: grid;
      gap: 12px;
    }

    .file-card {
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 16px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.78);
      box-shadow: 0 12px 24px rgba(15, 23, 42, 0.06);
    }

    .file-card header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      background: rgba(248, 250, 252, 0.84);
      border-bottom: 1px solid rgba(148, 163, 184, 0.2);
      font-weight: 600;
      color: #1e293b;
    }

    .file-card pre {
      margin: 0;
      padding: 14px;
      white-space: pre-wrap;
      overflow-x: auto;
      font-family: var(--font-mono);
      font-size: 0.88rem;
      line-height: 1.55;
      color: #0f172a;
    }

    .review-panel {
      display: none;
      padding: 14px;
      border-top: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(248, 250, 252, 0.92);
    }

    .review-panel.visible { display: block; }

    .review-panel .label {
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .review-panel textarea {
      display: none;
      min-height: 180px;
      font-family: var(--font-mono);
      margin-bottom: 10px;
    }

    .review-panel textarea.visible { display: block; }

    .review-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .review-actions button {
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.24);
      background: rgba(255, 255, 255, 0.82);
      color: #1e293b;
    }

    .review-actions .danger {
      border-color: rgba(225, 29, 72, 0.34);
      color: #9f1239;
      background: rgba(225, 29, 72, 0.1);
    }

    .review-modal {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(15, 23, 42, 0.22);
      z-index: 1000;
      padding: 24px;
      backdrop-filter: blur(8px);
    }

    .review-modal.open { display: flex; }

    .review-dialog {
      width: min(860px, 100%);
      max-height: 90vh;
      overflow: auto;
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.24);
      background: rgba(255, 255, 255, 0.9);
      box-shadow: var(--shadow-lg);
      padding: 18px;
    }

    .review-dialog h3 {
      margin: 0 0 10px;
      font-size: 1rem;
      color: #0f172a;
    }

    .review-dialog p {
      margin: 0 0 12px;
      color: var(--muted);
    }

    .review-dialog textarea {
      min-height: 300px;
      font-family: var(--font-mono);
      margin-bottom: 12px;
    }

    .check {
      color: var(--success);
      font-weight: 700;
    }

    .skeleton {
      display: grid;
      gap: 9px;
    }

    .skeleton-line {
      height: 11px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(226, 232, 240, 0.7), rgba(248, 250, 252, 0.95), rgba(226, 232, 240, 0.7));
      background-size: 200% 100%;
      animation: shimmer 1.3s linear infinite;
    }

    .skeleton-line.w-80 { width: 80%; }
    .skeleton-line.w-60 { width: 60%; }
    .skeleton-line.w-45 { width: 45%; }

    .code-skeleton {
      border-radius: 14px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.84);
      padding: 14px;
    }

    .status-bar {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 18px;
      align-items: center;
      padding: 12px 16px;
      border-radius: var(--radius-lg);
      background: var(--panel);
      box-shadow: var(--shadow-md);
      border: 1px solid rgba(148, 163, 184, 0.18);
      color: var(--muted);
      font-size: 0.88rem;
    }

    .status-main {
      color: var(--text);
      font-weight: 600;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.7);
      color: #334155;
      border: 1px solid rgba(148, 163, 184, 0.25);
      font-family: var(--font-mono);
    }

    .status-pill strong {
      color: var(--success);
      font-family: inherit;
    }

    .muted {
      color: var(--muted);
    }

    @keyframes shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }

    @keyframes blink {
      0%, 80%, 100% { opacity: 0.35; transform: scale(0.88); }
      40% { opacity: 1; transform: scale(1); }
    }

    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @keyframes floatIn {
      from { opacity: 0; transform: translateY(10px) scale(0.99); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }

    @media (max-width: 1024px) {
      .shell {
        grid-template-columns: 1fr;
      }

      .composer {
        position: static;
      }

      .plan-panel,
      .workspace {
        min-height: unset;
      }

      .tab-panel {
        max-height: 52vh;
      }

      .status-bar {
        grid-template-columns: 1fr;
        text-align: left;
      }
    }
  </style>
</head>
<body>
  <div id="cursor-glow"></div>
  <div class="auth-screen" id="auth-screen">
    <div class="auth-card">
      <div class="brand">
        <div class="brand-mark">&lt;/&gt;</div>
        <div>
          <div>dev_assistant</div>
          <div class="header-note">Secure workspace access</div>
        </div>
      </div>
      <h1>Welcome back</h1>
      <p>Sign in or create an account to access your generation history and usage dashboard.</p>
      <div class="field">
        <label for="auth-email">Email</label>
        <input id="auth-email" type="email" placeholder="you@example.com" />
      </div>
      <div class="field">
        <label for="auth-password">Password</label>
        <input id="auth-password" type="password" placeholder="Enter a secure password" />
      </div>
      <div class="button-row">
        <button class="primary" id="login-btn">Login</button>
        <button class="secondary" id="register-btn">Register</button>
      </div>
      <div class="banner" id="auth-banner"></div>
    </div>
  </div>

  <div class="app">
    <header class="header">
      <div class="brand">
        <div class="brand-mark">&lt;/&gt;</div>
        <div>
          <div>dev_assistant</div>
          <div class="header-note">Premium code generation cockpit</div>
        </div>
      </div>
      <div class="header-controls">
        <label class="review-toggle" for="review-mode-toggle">
          <span id="review-mode-label">Review Mode: OFF</span>
          <input id="review-mode-toggle" type="checkbox" />
        </label>
        <div class="status-chip dot" id="sandbox-badge">Checking sandbox</div>
        <div class="status-chip dot" id="connection-note">Ready</div>
        <button class="secondary" id="logout-btn" type="button">Logout</button>
      </div>
    </header>

    <div class="shell">
      <aside class="panel composer">
        <div class="usage-panel">
          <div class="usage-title">Usage Dashboard</div>
          <div class="usage-metric"><span>Plan tier</span><strong id="usage-plan">free</strong></div>
          <div class="usage-metric"><span>Daily budget</span><strong id="usage-calls">0 / 5</strong></div>
          <div class="usage-meter"><div id="usage-meter-fill"></div></div>
          <div class="usage-warning hidden" id="usage-warning">You are near your daily limit.</div>
        </div>

        <div class="field-stack">
          <div class="field">
            <label for="prompt">Prompt</label>
            <textarea id="prompt" placeholder="Describe what you want to build..."></textarea>
            <div class="suggestions" id="prompt-suggestions">
              <div class="suggestion-chip" data-suggestion="Build a modern task management dashboard with filters, drag-and-drop cards, and analytics widgets.">Task dashboard</div>
              <div class="suggestion-chip" data-suggestion="Create a collaborative markdown editor with live preview, version history, and keyboard shortcuts.">Markdown studio</div>
              <div class="suggestion-chip" data-suggestion="Generate a minimal API starter with auth, pagination, and structured logging.">API starter</div>
            </div>
          </div>

          <div class="field">
            <label for="model">Model</label>
            <select id="model"></select>
          </div>

          <div class="field">
            <label for="output-dir">Output directory</label>
            <input id="output-dir" type="text" value="./output" />
          </div>

          <div class="button-row">
            <button class="primary" id="generate-btn">Generate</button>
            <button class="secondary" id="clear-btn">Reset</button>
          </div>
        </div>
      </aside>

      <main class="panel plan-panel">
        <div id="banner" class="banner"></div>

        <div class="thinking-card">
          <div class="heading-row">
            <h2 class="section-title">AI Thinking Panel</h2>
            <div class="typing-indicator hidden" id="typing-indicator">
              <span></span><span></span><span></span>
              <strong>Streaming</strong>
            </div>
          </div>
          <p class="hint">Plan output streams here in real time before file generation begins.</p>
          <div id="plan-box" class="stream-box"></div>
        </div>
      </main>

      <section class="panel workspace">
        <div class="tabs">
          <button class="tab-btn active" id="tab-files-btn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h6l2 2h10v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>
            Files
          </button>
          <button class="tab-btn" id="tab-code-btn" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m8 16-4-4 4-4"/><path d="m16 8 4 4-4 4"/></svg>
            Code
          </button>
        </div>

        <div class="tab-panel" id="files-panel">
          <p class="hint">Generated files are listed here as the architecture resolves.</p>
          <div id="file-list" class="chips"></div>
        </div>

        <div class="tab-panel hidden" id="code-panel">
          <p class="hint">Live code blocks appear here with file-level review controls.</p>
          <div id="code-list" class="code-list"></div>
        </div>
      </section>
    </div>

    <footer class="status-bar">
      <div class="status-main">
        <strong id="status-badge" class="status-chip dot">Idle</strong>
        <span id="status-text" style="margin-left:10px;">Ready to generate</span>
      </div>
      <div class="status-pill" id="progress-text">Files: 0 / 0 complete</div>
      <div class="status-pill" id="elapsed-text">Elapsed: 00:00</div>
    </footer>

    <div class="review-modal" id="plan-review-modal" role="dialog" aria-modal="true" aria-labelledby="plan-review-title" aria-hidden="true">
      <div class="review-dialog">
        <h3 id="plan-review-title">Plan Review Required</h3>
        <p>Review the generated plan before generation continues.</p>
        <textarea id="plan-review-text" readonly></textarea>
        <div class="review-actions">
          <button id="plan-approve-btn">✅ Approve</button>
          <button id="plan-edit-btn">✏️ Edit &amp; Continue</button>
          <button id="plan-cancel-btn" class="danger">❌ Cancel Generation</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    const promptEl = document.getElementById('prompt');
    const modelEl = document.getElementById('model');
    const outputDirEl = document.getElementById('output-dir');
    const generateBtn = document.getElementById('generate-btn');
    const clearBtn = document.getElementById('clear-btn');
    const planBox = document.getElementById('plan-box');
    const fileListEl = document.getElementById('file-list');
    const codeListEl = document.getElementById('code-list');
    const statusText = document.getElementById('status-text');
    const progressText = document.getElementById('progress-text');
    const elapsedText = document.getElementById('elapsed-text');
    const bannerEl = document.getElementById('banner');
    const sandboxBadge = document.getElementById('sandbox-badge');
    const connectionNote = document.getElementById('connection-note');
    const reviewModeToggle = document.getElementById('review-mode-toggle');
    const reviewModeLabel = document.getElementById('review-mode-label');
    const planReviewModal = document.getElementById('plan-review-modal');
    const planReviewText = document.getElementById('plan-review-text');
    const planApproveBtn = document.getElementById('plan-approve-btn');
    const planEditBtn = document.getElementById('plan-edit-btn');
    const planCancelBtn = document.getElementById('plan-cancel-btn');
    const authScreen = document.getElementById('auth-screen');
    const authEmail = document.getElementById('auth-email');
    const authPassword = document.getElementById('auth-password');
    const loginBtn = document.getElementById('login-btn');
    const registerBtn = document.getElementById('register-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const authBanner = document.getElementById('auth-banner');
    const usagePlanEl = document.getElementById('usage-plan');
    const usageCallsEl = document.getElementById('usage-calls');
    const usageMeterFill = document.getElementById('usage-meter-fill');
    const usageWarning = document.getElementById('usage-warning');
    const tabFilesBtn = document.getElementById('tab-files-btn');
    const tabCodeBtn = document.getElementById('tab-code-btn');
    const filesPanel = document.getElementById('files-panel');
    const codePanel = document.getElementById('code-panel');
    const typingIndicator = document.getElementById('typing-indicator');
    const statusBadge = document.getElementById('status-badge');
    const promptSuggestions = document.querySelectorAll('.suggestion-chip');
    const cursorGlow = document.getElementById('cursor-glow');

    const API_KEY_STORAGE = 'dev_assistant_api_key';
    const USER_STORAGE = 'dev_assistant_user';

    let activeSource = null;
    let elapsedTimer = null;
    let startedAt = 0;
    let completedFiles = 0;
    let totalFiles = 0;
    let currentFileBlocks = new Map();
    let currentModelValue = '';
    let pendingPlanReview = null;
    let escapeReviewHandler = null;
    let pendingPollTimer = null;
    let seenReviewIds = new Set();
    let hasPlanStreamingStarted = false;
    let hasCodeStreamingStarted = false;

    document.addEventListener('mousemove', (event) => {
      if (!cursorGlow) {
        return;
      }
      cursorGlow.style.transform = `translate(${event.clientX - 140}px, ${event.clientY - 140}px)`;
    });

    function formatElapsed(ms) {
      const totalSeconds = Math.floor(ms / 1000);
      const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
      const seconds = String(totalSeconds % 60).padStart(2, '0');
      return `${minutes}:${seconds}`;
    }

    function setStatus(message) {
      statusText.textContent = message;
      const lower = String(message || '').toLowerCase();
      if (lower.includes('error')) {
        statusBadge.textContent = 'Error';
        statusBadge.style.color = '#be123c';
      } else if (lower.includes('done') || lower.includes('complete')) {
        statusBadge.textContent = 'Done';
        statusBadge.style.color = '#0f766e';
      } else if (lower.includes('cancel')) {
        statusBadge.textContent = 'Cancelled';
        statusBadge.style.color = '#b45309';
      } else if (lower.includes('generating') || lower.includes('starting') || lower.includes('stream')) {
        statusBadge.textContent = 'Generating';
        statusBadge.style.color = '#134e4a';
      } else {
        statusBadge.textContent = 'Idle';
        statusBadge.style.color = '#334155';
      }
    }

    function setTyping(active) {
      typingIndicator.classList.toggle('hidden', !active);
    }

    function setWorkspaceTab(tab) {
      const filesActive = tab === 'files';
      tabFilesBtn.classList.toggle('active', filesActive);
      tabCodeBtn.classList.toggle('active', !filesActive);
      filesPanel.classList.toggle('hidden', !filesActive);
      codePanel.classList.toggle('hidden', filesActive);
    }

    function getApiKey() {
      return localStorage.getItem(API_KEY_STORAGE) || '';
    }

    function setApiKey(apiKey, user) {
      localStorage.setItem(API_KEY_STORAGE, apiKey);
      if (user) {
        localStorage.setItem(USER_STORAGE, JSON.stringify(user));
      }
      document.body.classList.add('authenticated');
      authScreen.classList.add('hidden');
    }

    function clearApiKey() {
      localStorage.removeItem(API_KEY_STORAGE);
      localStorage.removeItem(USER_STORAGE);
      document.body.classList.remove('authenticated');
      authScreen.classList.remove('hidden');
    }

    function logoutSession() {
      closeSource();
      stopTimer();
      setRunning(false);
      clearApiKey();
      resetOutputs();
      setWorkspaceTab('files');
      setStatus('Signed out');
      connectionNote.textContent = 'Signed out';
      authPassword.value = '';
      setAuthBanner('success', 'Logged out successfully.');
    }

    function setAuthBanner(type, message) {
      authBanner.className = `banner visible ${type}`;
      authBanner.textContent = message;
    }

    function clearAuthBanner() {
      authBanner.className = 'banner';
      authBanner.textContent = '';
    }

    function getAuthHeaders(extra = {}) {
      const apiKey = getApiKey();
      return apiKey ? { ...extra, Authorization: `Bearer ${apiKey}` } : { ...extra };
    }

    async function authFetch(url, options = {}) {
      const headers = new Headers(options.headers || {});
      const authHeaders = getAuthHeaders();
      Object.entries(authHeaders).forEach(([key, value]) => headers.set(key, value));
      const response = await fetch(url, { ...options, headers });
      if (response.status === 401) {
        clearApiKey();
        setAuthBanner('error', 'Session expired. Please log in again.');
      }
      return response;
    }

    async function submitAuth(endpoint) {
      const email = authEmail.value.trim();
      const password = authPassword.value.trim();
      if (!email || !password) {
        setAuthBanner('error', 'Email and password are required.');
        return;
      }
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || 'Authentication failed');
      }
      setApiKey(payload.api_key, payload.user);
      clearAuthBanner();
      await Promise.all([loadModels(), loadUsage(), loadSandboxStatus()]);
      setStatus('Ready');
    }

    function updateUsageDashboard(data) {
      usagePlanEl.textContent = data.plan || 'free';
      usageCallsEl.textContent = `${data.api_calls_today || 0} / ${data.daily_limit || 0}`;
      const limit = Number(data.daily_limit || 0);
      const calls = Number(data.api_calls_today || 0);
      const percent = limit > 0 ? Math.min((calls / limit) * 100, 100) : 0;
      usageMeterFill.style.width = `${percent}%`;
      usageWarning.classList.toggle('hidden', percent < 80);
      if (percent >= 80) {
        usageWarning.textContent = 'Near daily cap. Consider upgrading your plan.';
      }
    }

    async function loadUsage() {
      const response = await authFetch('/usage');
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      updateUsageDashboard(data);
    }

    function isReviewModeEnabled() {
      return Boolean(reviewModeToggle.checked);
    }

    function setReviewModeLabel() {
      reviewModeLabel.textContent = `Review Mode: ${isReviewModeEnabled() ? 'ON' : 'OFF'}`;
    }

    async function submitReview(reviewId, action, editedContent = null, comment = null) {
      const response = await authFetch(`/review/${encodeURIComponent(reviewId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, edited_content: editedContent, comment }),
      });
      if (!response.ok) {
        throw new Error(`Review submission failed (${response.status})`);
      }
    }

    function setBanner(type, message) {
      bannerEl.className = `banner visible ${type}`;
      bannerEl.textContent = message;
    }

    function clearBanner() {
      bannerEl.className = 'banner';
      bannerEl.textContent = '';
    }

    function scrollCodeToBottom() {
      codeListEl.scrollTop = codeListEl.scrollHeight;
    }

    function resetOutputs() {
      planBox.textContent = '';
      fileListEl.innerHTML = '';
      codeListEl.innerHTML = '';
      clearBanner();
      completedFiles = 0;
      totalFiles = 0;
      currentFileBlocks = new Map();
      progressText.textContent = 'Files: 0 / 0 complete';
      elapsedText.textContent = 'Elapsed: 00:00';
      setStatus('Ready to generate');
      connectionNote.textContent = 'Ready';
      pendingPlanReview = null;
      closePlanReviewModal();
      seenReviewIds = new Set();
      hasPlanStreamingStarted = false;
      hasCodeStreamingStarted = false;
      setTyping(false);
      showSkeletons();
    }

    function showSkeletons() {
      planBox.innerHTML = `
        <div class="skeleton">
          <div class="skeleton-line w-80"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line w-60"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line w-45"></div>
        </div>
      `;
      codeListEl.innerHTML = `
        <div class="code-skeleton">
          <div class="skeleton">
            <div class="skeleton-line w-60"></div>
            <div class="skeleton-line"></div>
            <div class="skeleton-line"></div>
            <div class="skeleton-line w-80"></div>
          </div>
        </div>
      `;
    }

    async function loadSandboxStatus() {
      try {
        const response = await authFetch('/sandbox/status');
        const status = await response.json();
        const mode = (status.mode || 'auto').toLowerCase();
        const dockerAvailable = Boolean(status.docker_available);
        const effectiveDocker = mode === 'docker' ? dockerAvailable : (mode === 'auto' ? dockerAvailable : false);
        if (effectiveDocker) {
          sandboxBadge.textContent = 'Docker sandbox';
          sandboxBadge.style.color = '#0f766e';
        } else {
          sandboxBadge.textContent = 'Subprocess (unsafe)';
          sandboxBadge.style.color = '#b45309';
        }
      } catch (error) {
        sandboxBadge.textContent = 'Subprocess (unsafe)';
        sandboxBadge.style.color = '#b45309';
      }
    }

    function startTimer() {
      startedAt = Date.now();
      if (elapsedTimer) {
        clearInterval(elapsedTimer);
      }
      elapsedText.textContent = 'Elapsed: 00:00';
      elapsedTimer = setInterval(() => {
        elapsedText.textContent = `Elapsed: ${formatElapsed(Date.now() - startedAt)}`;
      }, 1000);
    }

    function stopTimer() {
      if (elapsedTimer) {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
      }
    }

    function setRunning(isRunning) {
      generateBtn.disabled = isRunning;
      clearBtn.disabled = isRunning;
      promptEl.disabled = isRunning;
      modelEl.disabled = isRunning;
      outputDirEl.disabled = isRunning;
    }

    function trapPlanModalFocus(event) {
      if (event.key === 'Escape' && pendingPlanReview) {
        event.preventDefault();
        approvePlanReview();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }

      const focusable = [planApproveBtn, planEditBtn, planCancelBtn, planReviewText].filter((el) => !el.disabled);
      if (focusable.length === 0) {
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function openPlanReviewModal(payload) {
      pendingPlanReview = payload;
      planReviewText.value = payload.content || '';
      planReviewText.readOnly = true;
      planEditBtn.dataset.editMode = 'false';
      planEditBtn.textContent = '✏️ Edit & Continue';

      planReviewModal.classList.add('open');
      planReviewModal.setAttribute('aria-hidden', 'false');
      escapeReviewHandler = trapPlanModalFocus;
      document.addEventListener('keydown', escapeReviewHandler);
      planApproveBtn.focus();
      setStatus('Waiting for plan review...');
    }

    function closePlanReviewModal() {
      planReviewModal.classList.remove('open');
      planReviewModal.setAttribute('aria-hidden', 'true');
      if (escapeReviewHandler) {
        document.removeEventListener('keydown', escapeReviewHandler);
        escapeReviewHandler = null;
      }
    }

    async function approvePlanReview() {
      if (!pendingPlanReview) {
        return;
      }
      const reviewId = pendingPlanReview.review_id;
      pendingPlanReview = null;
      closePlanReviewModal();
      await submitReview(reviewId, 'approve', null, 'Approved from modal');
      setStatus('Resuming generation...');
    }

    async function cancelPlanReview() {
      if (!pendingPlanReview) {
        return;
      }
      const reviewId = pendingPlanReview.review_id;
      pendingPlanReview = null;
      closePlanReviewModal();
      await submitReview(reviewId, 'reject_all', null, 'Cancelled from modal');
      setStatus('Generation cancelled by reviewer');
    }

    async function handlePlanEditContinue() {
      if (!pendingPlanReview) {
        return;
      }
      const inEditMode = planEditBtn.dataset.editMode === 'true';
      if (!inEditMode) {
        planReviewText.readOnly = false;
        planEditBtn.dataset.editMode = 'true';
        planEditBtn.textContent = '✅ Confirm Edit & Continue';
        planReviewText.focus();
        return;
      }

      const reviewId = pendingPlanReview.review_id;
      const edited = planReviewText.value;
      pendingPlanReview = null;
      closePlanReviewModal();
      await submitReview(reviewId, 'edit', edited, 'Edited from modal');
      setStatus('Resuming generation...');
    }

    function ensureFileBlock(fileName) {
      if (currentFileBlocks.has(fileName)) {
        return currentFileBlocks.get(fileName);
      }

      const wrapper = document.createElement('article');
      wrapper.className = 'file-card';

      const header = document.createElement('header');
      const title = document.createElement('span');
      title.textContent = fileName;
      const status = document.createElement('span');
      status.className = 'muted';
      status.textContent = 'Streaming...';
      header.appendChild(title);
      header.appendChild(status);

      const pre = document.createElement('pre');
      pre.textContent = '';

      const reviewPanel = document.createElement('div');
      reviewPanel.className = 'review-panel';
      const label = document.createElement('div');
      label.className = 'label';
      label.textContent = 'Review this file before writing to disk';

      const editor = document.createElement('textarea');

      const actions = document.createElement('div');
      actions.className = 'review-actions';
      const approveBtn = document.createElement('button');
      approveBtn.textContent = '✅ Write to disk';
      const editBtn = document.createElement('button');
      editBtn.textContent = '✏️ Edit code';
      const skipBtn = document.createElement('button');
      skipBtn.textContent = '⏭ Skip file';

      actions.appendChild(approveBtn);
      actions.appendChild(editBtn);
      actions.appendChild(skipBtn);
      reviewPanel.appendChild(label);
      reviewPanel.appendChild(editor);
      reviewPanel.appendChild(actions);

      wrapper.appendChild(header);
      wrapper.appendChild(pre);
      wrapper.appendChild(reviewPanel);
      codeListEl.appendChild(wrapper);
      currentFileBlocks.set(fileName, {
        wrapper,
        header,
        pre,
        status,
        reviewPanel,
        reviewLabel: label,
        reviewEditor: editor,
        approveBtn,
        editBtn,
        skipBtn,
        activeReviewId: null,
      });
      return currentFileBlocks.get(fileName);
    }

    function hideFileReviewPanel(fileName) {
      const block = ensureFileBlock(fileName);
      block.reviewPanel.classList.remove('visible');
      block.reviewEditor.classList.remove('visible');
      block.reviewEditor.value = '';
      block.editBtn.dataset.editMode = 'false';
      block.editBtn.textContent = '✏️ Edit code';
      block.activeReviewId = null;
    }

    function showFileReviewPanel(payload) {
      const fileName = payload.file_path || payload.file;
      const block = ensureFileBlock(fileName);
      block.reviewPanel.classList.add('visible');
      block.reviewLabel.textContent = payload.message || `Review ${fileName} before writing to disk`;
      block.activeReviewId = payload.review_id;

      block.approveBtn.onclick = async () => {
        await submitReview(payload.review_id, 'approve', null, 'Write to disk from file panel');
        hideFileReviewPanel(fileName);
        setStatus('Resuming generation...');
      };

      block.skipBtn.onclick = async () => {
        await submitReview(payload.review_id, 'skip', null, 'Skipped from file panel');
        hideFileReviewPanel(fileName);
        setStatus('Resuming generation...');
      };

      block.editBtn.onclick = async () => {
        const inEditMode = block.editBtn.dataset.editMode === 'true';
        if (!inEditMode) {
          block.reviewEditor.value = payload.content || block.pre.textContent;
          block.reviewEditor.classList.add('visible');
          block.editBtn.dataset.editMode = 'true';
          block.editBtn.textContent = '✅ Confirm Edit';
          block.reviewEditor.focus();
          return;
        }
        await submitReview(payload.review_id, 'edit', block.reviewEditor.value, 'Edited from file panel');
        hideFileReviewPanel(fileName);
        setStatus('Resuming generation...');
      };
    }

    async function handleReviewRequired(payload) {
      if (!payload || !payload.review_id) {
        return;
      }
      if (seenReviewIds.has(payload.review_id)) {
        return;
      }
      seenReviewIds.add(payload.review_id);

      if (!isReviewModeEnabled()) {
        await submitReview(payload.review_id, 'approve', null, 'Auto-approved because review mode is OFF');
        setStatus('Resuming generation...');
        return;
      }

      if (payload.review_type === 'plan') {
        openPlanReviewModal(payload);
      } else if (payload.review_type === 'file') {
        showFileReviewPanel(payload);
      }
    }

    async function pollPendingReviews() {
      if (!activeSource) {
        return;
      }
      try {
        const response = await authFetch('/reviews/pending');
        if (!response.ok) {
          return;
        }
        const pending = await response.json();
        for (const item of pending) {
          await handleReviewRequired({
            ...item,
            message: item.review_type === 'plan'
              ? 'Review the plan before continuing'
              : `Review ${item.file_path || 'file'} before writing to disk`,
            file: item.file_path,
          });
        }
      } catch (error) {
        // Silent fallback polling; SSE remains primary source of events.
      }
    }

    function renderFileList(files) {
      fileListEl.innerHTML = '';
      files.forEach((file) => {
        const chip = document.createElement('div');
        chip.className = 'chip';
        chip.textContent = file;
        fileListEl.appendChild(chip);
      });
    }

    function appendPlanText(content) {
      planBox.textContent += content;
      planBox.scrollTop = planBox.scrollHeight;
    }

    function appendCodeText(fileName, content) {
      const block = ensureFileBlock(fileName);
      block.pre.textContent += content;
      scrollCodeToBottom();
    }

    function markFileDone(fileName) {
      const block = ensureFileBlock(fileName);
      block.status.innerHTML = '<span class="check">✅</span> Done';
      completedFiles += 1;
      progressText.textContent = `Files: ${completedFiles} / ${totalFiles} complete`;
    }

    function parseSseEvent(raw) {
      try {
        return JSON.parse(raw);
      } catch (error) {
        return null;
      }
    }

    async function loadModels() {
      modelEl.innerHTML = '<option value="">Loading models...</option>';
      try {
        const response = await authFetch('/models');
        if (!response.ok) {
          throw new Error('Failed to load models');
        }
        const models = await response.json();
        modelEl.innerHTML = '';

        const defaultOption = document.createElement('option');
        defaultOption.value = '';
        defaultOption.textContent = 'Use step defaults';
        modelEl.appendChild(defaultOption);

        models.forEach((item) => {
          const option = document.createElement('option');
          option.value = item.id || item.name;
          const pricing = item.pricing || {};
          const priceBits = [];
          if (pricing.prompt) {
            priceBits.push(`prompt ${pricing.prompt}`);
          }
          if (pricing.completion) {
            priceBits.push(`completion ${pricing.completion}`);
          }
          option.textContent = priceBits.length ? `${item.name || item.id} (${priceBits.join(', ')})` : (item.name || item.id);
          modelEl.appendChild(option);
        });
      } catch (error) {
        modelEl.innerHTML = '';
        const fallback = document.createElement('option');
        fallback.value = '';
        fallback.textContent = 'Use step defaults';
        modelEl.appendChild(fallback);
        const fallbackModel = document.createElement('option');
        fallbackModel.value = 'openai/gpt-oss-20b:free';
        fallbackModel.textContent = 'openai/gpt-oss-20b:free (fallback)';
        modelEl.appendChild(fallbackModel);
      }
    }

    function closeSource() {
      if (activeSource) {
        if (typeof activeSource.close === 'function') {
          activeSource.close();
        }
        if (typeof activeSource.abort === 'function') {
          activeSource.abort();
        }
        activeSource = null;
      }
      if (pendingPollTimer) {
        clearInterval(pendingPollTimer);
        pendingPollTimer = null;
      }
    }

    function handleGenerationDone(outputDir) {
      stopTimer();
      setRunning(false);
      setStatus('Done!');
      connectionNote.textContent = 'Idle';
      setBanner('success', `Generation complete. Files were written to ${outputDir}`);
      closePlanReviewModal();
      closeSource();
      setTyping(false);
      loadUsage().catch(() => {});
    }

    function handleGenerationError(message) {
      stopTimer();
      setRunning(false);
      setStatus('Error');
      connectionNote.textContent = 'Disconnected';
      setBanner('error', message);
      closePlanReviewModal();
      closeSource();
      setTyping(false);
    }

    async function startGeneration() {
      const prompt = promptEl.value.trim();
      const outputDir = outputDirEl.value.trim() || './output';
      currentModelValue = modelEl.value.trim();

      if (!getApiKey()) {
        setAuthBanner('error', 'Please log in or register first.');
        authScreen.classList.remove('hidden');
        return;
      }

      if (!prompt) {
        setBanner('error', 'Please enter a prompt before generating.');
        return;
      }

      resetOutputs();
      setRunning(true);
      clearBanner();
      setStatus('Starting...');
      connectionNote.textContent = 'Connecting';
      connectionNote.style.color = '#134e4a';
      startTimer();
      setWorkspaceTab('files');
      showSkeletons();
      setTyping(true);

      const abortController = new AbortController();
      activeSource = abortController;
      pendingPollTimer = setInterval(pollPendingReviews, 4000);

      try {
        const response = await authFetch('/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, output_dir: outputDir, model: currentModelValue || null }),
          signal: abortController.signal,
        });

        if (!response.ok || !response.body) {
          const errorBody = await response.text();
          throw new Error(errorBody || `Generation failed (${response.status})`);
        }

        connectionNote.textContent = 'Streaming';
        connectionNote.style.color = '#134e4a';
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          let separatorIndex = buffer.indexOf('\n\n');
          while (separatorIndex !== -1) {
            const rawEvent = buffer.slice(0, separatorIndex).trim();
            buffer = buffer.slice(separatorIndex + 2);
            if (rawEvent.startsWith('data: ')) {
              const payload = parseSseEvent(rawEvent.slice(6));
              if (payload) {
                if (payload.type === 'status') {
                  setStatus(payload.message);
                  if (String(payload.message || '').toLowerCase().includes('generating')) {
                    setWorkspaceTab('code');
                  }
                } else if (payload.type === 'plan_chunk') {
                  if (!hasPlanStreamingStarted) {
                    planBox.textContent = '';
                    hasPlanStreamingStarted = true;
                  }
                  appendPlanText(payload.content || '');
                } else if (payload.type === 'file_list') {
                  totalFiles = Array.isArray(payload.files) ? payload.files.length : 0;
                  progressText.textContent = `Files: ${completedFiles} / ${totalFiles} complete`;
                  renderFileList(payload.files || []);
                } else if (payload.type === 'file_start') {
                  if (!hasCodeStreamingStarted) {
                    codeListEl.innerHTML = '';
                    hasCodeStreamingStarted = true;
                  }
                  ensureFileBlock(payload.file);
                } else if (payload.type === 'code_chunk') {
                  if (!hasCodeStreamingStarted) {
                    codeListEl.innerHTML = '';
                    hasCodeStreamingStarted = true;
                  }
                  appendCodeText(payload.file, payload.content || '');
                } else if (payload.type === 'file_done') {
                  markFileDone(payload.file);
                  hideFileReviewPanel(payload.file);
                } else if (payload.type === 'file_skipped') {
                  hideFileReviewPanel(payload.file);
                  setStatus(`Skipped ${payload.file}`);
                } else if (payload.type === 'review_required') {
                  handleReviewRequired(payload).catch((error) => {
                    handleGenerationError(error.message || 'Review action failed');
                  });
                } else if (payload.type === 'review_resolved') {
                  setStatus('Resuming generation...');
                } else if (payload.type === 'cancelled') {
                  stopTimer();
                  setRunning(false);
                  setStatus('Cancelled');
                  connectionNote.textContent = 'Idle';
                  setBanner('error', payload.message || 'Generation cancelled');
                  closePlanReviewModal();
                  closeSource();
                  setTyping(false);
                } else if (payload.type === 'done') {
                  handleGenerationDone(payload.output_dir || outputDir);
                } else if (payload.type === 'error') {
                  handleGenerationError(payload.message || 'Something failed');
                }
              }
            }
            separatorIndex = buffer.indexOf('\n\n');
          }
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          handleGenerationError(error.message || 'Something failed');
        }
      }
    }

    clearBtn.addEventListener('click', () => {
      closeSource();
      stopTimer();
      promptEl.value = '';
      outputDirEl.value = './output';
      setRunning(false);
      resetOutputs();
      setWorkspaceTab('files');
    });

    planApproveBtn.addEventListener('click', () => {
      approvePlanReview().catch((error) => handleGenerationError(error.message || 'Review approval failed'));
    });
    planEditBtn.addEventListener('click', () => {
      handlePlanEditContinue().catch((error) => handleGenerationError(error.message || 'Plan edit failed'));
    });
    planCancelBtn.addEventListener('click', () => {
      cancelPlanReview().catch((error) => handleGenerationError(error.message || 'Cancellation failed'));
    });

    reviewModeToggle.addEventListener('change', setReviewModeLabel);

    tabFilesBtn.addEventListener('click', () => setWorkspaceTab('files'));
    tabCodeBtn.addEventListener('click', () => setWorkspaceTab('code'));

    promptSuggestions.forEach((chip) => {
      chip.addEventListener('click', () => {
        const value = chip.getAttribute('data-suggestion') || '';
        promptEl.value = value;
        promptEl.focus();
      });
    });

    loginBtn.addEventListener('click', () => {
      submitAuth('/auth/login').catch((error) => setAuthBanner('error', error.message || 'Login failed'));
    });

    registerBtn.addEventListener('click', () => {
      submitAuth('/auth/register').catch((error) => setAuthBanner('error', error.message || 'Registration failed'));
    });

    logoutBtn.addEventListener('click', logoutSession);

    authPassword.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        submitAuth('/auth/login').catch((error) => setAuthBanner('error', error.message || 'Login failed'));
      }
    });

    function bootstrapAuth() {
      if (getApiKey()) {
        document.body.classList.add('authenticated');
        authScreen.classList.add('hidden');
      } else {
        document.body.classList.remove('authenticated');
        authScreen.classList.remove('hidden');
      }
    }

    generateBtn.addEventListener('click', startGeneration);

    bootstrapAuth();
    loadSandboxStatus().catch(() => {});
    if (getApiKey()) {
      loadModels().catch(() => {});
      loadUsage().catch(() => {});
    }
    setReviewModeLabel();
    setWorkspaceTab('files');
    showSkeletons();
    setTyping(false);
  </script>
</body>
</html>
"""
