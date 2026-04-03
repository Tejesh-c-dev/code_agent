# Quick note: one-line comment added as requested.
"""OpenRouter chat completion client with step-aware multi-model fallback support."""

import json
import logging
import os
from typing import Optional, List

import requests

from dev_assistant.model_config import get_model_candidates


logger = logging.getLogger(__name__)


class OpenRouterError(Exception):
    pass


class OpenRouterCompletion:
    BASE_URL = "https://openrouter.ai/api/v1"

    @staticmethod
    def _is_retryable_model_error(response: Optional[requests.Response]) -> bool:
        if response is None:
            return False

        try:
            text = response.text.lower()
        except Exception:
            text = ""

        retryable_phrases = (
            "rate limit",
            "model not available",
            "model unavailable",
            "model is not available",
            "not available",
            "invalid model",
            "unknown model",
            "no endpoints found",
        )
        return response.status_code in (400, 404, 429, 503) or any(phrase in text for phrase in retryable_phrases)

    @staticmethod
    def _format_http_error(response: Optional[requests.Response], model: str) -> str:
        if response is None:
            return f"OpenRouter request for model '{model}' failed with no response"

        status = response.status_code
        message = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                err = payload.get("error", {})
                if isinstance(err, dict):
                    message = err.get("message", "")
                elif isinstance(err, str):
                    message = err
        except Exception:
            message = ""

        if not message:
            message = (response.text or "").strip()
        if len(message) > 300:
            message = f"{message[:300]}..."

        return f"OpenRouter request failed for model '{model}' ({status}): {message}"

    @staticmethod
    def _post(url: str, payload: dict, headers: dict, stream: bool) -> requests.Response:
        response = requests.post(url, json=payload, headers=headers, stream=stream, timeout=60)
        response.raise_for_status()
        return response

    @staticmethod
    def create(
        model: str,
        messages: List[dict],
        temperature: float = 0.7,
        stream: bool = False,
        functions: Optional[List] = None,
        function_call: Optional[dict] = None,
        step: Optional[str] = None,
    ):
        """Create a chat completion using OpenRouter API."""

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise OpenRouterError("OPENROUTER_API_KEY environment variable not set")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/smol-ai/developer",
            "X-Title": "smol-developer",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }

        # Note: OpenRouter has limited function calling support.
        # We keep the request shape compatible with the existing code.
        if functions:
            payload["functions"] = functions
        if function_call:
            payload["function_call"] = function_call

        url = f"{OpenRouterCompletion.BASE_URL}/chat/completions"

        candidates = get_model_candidates(step, model) if step else [model]
        last_error: Optional[OpenRouterError] = None

        for idx, candidate in enumerate(candidates):
            payload["model"] = candidate
            try:
                response = OpenRouterCompletion._post(url, payload, headers, stream)
                break
            except requests.HTTPError as exc:
                formatted = OpenRouterCompletion._format_http_error(exc.response, candidate)
                last_error = OpenRouterError(formatted)
                retryable = OpenRouterCompletion._is_retryable_model_error(exc.response)
                has_next = idx < len(candidates) - 1
                if retryable and has_next:
                    logger.warning("Model %s unavailable, falling back to %s", candidate, candidates[idx + 1])
                    continue
                raise last_error from exc
        else:
            raise last_error or OpenRouterError(f"OpenRouter request failed for model '{model}'")

        if stream:
            return OpenRouterStreamingCompletion(response)
        return response.json()


class OpenRouterStreamingCompletion:
    def __init__(self, response):
        self.response = response
        self.usage = None

    def iter_lines(self):
        for line_data in self.response.iter_lines():
            if not line_data:
                continue
            try:
                line_str = line_data.decode("utf-8")
            except Exception:
                yield line_data
                continue
            if line_str.startswith("data: "):
                try:
                    chunk = json.loads(line_str[6:])
                    usage = chunk.get("usage")
                    if usage:
                        self.usage = usage
                except json.JSONDecodeError:
                    pass
            yield line_data

    def raise_for_status(self):
        return self.response.raise_for_status()

    def __iter__(self):
        for line_data in self.iter_lines():
            if line_data:
                line_str = line_data.decode("utf-8")
                if line_str.startswith("data: "):
                    try:
                        chunk = json.loads(line_str[6:])
                        usage = chunk.get("usage")
                        if usage:
                            self.usage = usage
                        yield chunk
                    except json.JSONDecodeError:
                        continue
