# Quick note: one-line comment added as requested.
"""Prompt orchestration helpers that route each step through the configured model."""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, List, Optional

from dotenv import load_dotenv
from openai_function_call import openai_function
from tenacity import retry, stop_after_attempt, wait_random_exponential

from dev_assistant.model_config import get_model
from dev_assistant.openrouter_client import OpenRouterCompletion


logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

SMOL_DEV_SYSTEM_PROMPT = """
You are a top tier AI developer who is trying to write a program that will generate code for the user based on their intent.
Do not leave any todos, fully implement every feature requested.

When writing code, add comments to explain what you intend to do and why it aligns with the program plan and specific instructions from the original prompt.
"""

API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("OPENROUTER_API_KEY environment variable not set. Please set it in your .env file.")


@openai_function
def file_paths(files_to_edit: List[str]) -> List[str]:
    """Construct a list of strings."""

    return files_to_edit


def _sanitize_path_token(token: str) -> str:
    # Remove common markdown/list punctuation around a path token.
    cleaned = token.strip().strip("`\"'.,:;()[]{}")
    cleaned = cleaned.replace("\\", "/")
    cleaned = cleaned.lstrip("*-_#>")
    cleaned = re.sub(r"[^A-Za-z0-9_./-]", "", cleaned)
    return cleaned


def _extract_file_paths(content: str) -> List[str]:
    candidates: List[str] = []

    # Common plan output styles, e.g.:
    # - index.html:
    # 1. `styles.css`
    # "script.js",
    patterns = [
        r"^\s*-\s*[`\"']?([^`\"':,\n]+\.[A-Za-z0-9]+)[`\"']?\s*:?.*$",
        r"^\s*\d+\.\s*[`\"']?([^`\"',\n]+\.[A-Za-z0-9]+)[`\"']?.*$",
        r"[`\"']([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)[`\"']",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, content, flags=re.MULTILINE):
            candidates.append(_sanitize_path_token(match))

    # If patterns above found nothing, fall back to token scanning.
    if not candidates:
        for token in re.split(r"\s+", content):
            cleaned = _sanitize_path_token(token)
            if re.match(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]+$", cleaned):
                candidates.append(cleaned)

    # Keep order while removing duplicates and obvious non-file strings.
    allowed_ext = {
        "html", "css", "js", "jsx", "ts", "tsx", "py", "json", "md",
        "txt", "yml", "yaml", "toml", "xml", "sh", "sql",
    }
    seen = set()
    result: List[str] = []
    for path in candidates:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in allowed_ext:
            continue
        if path and path not in seen:
            seen.add(path)
            result.append(path)

    return result


def _create_completion(messages: List[dict], model: str, *, stream: bool, step: str):
    return OpenRouterCompletion.create(
        model=model,
        messages=messages,
        temperature=0.7,
        stream=stream,
        step=step,
    )


def specify_file_paths(
    prompt: str,
    plan: str,
    model: str = get_model("specify_file_paths"),
    return_usage: bool = False,
):
    messages = [
        {
            "role": "system",
            "content": f"""{SMOL_DEV_SYSTEM_PROMPT}
Given the prompt and the plan, return a list of strings corresponding to the new files that will be generated.
                  """,
        },
        {
            "role": "user",
            "content": f""" I want a: {prompt} """,
        },
        {
            "role": "user",
            "content": f""" The plan we have agreed on is: {plan} """,
        },
    ]

    result = _create_completion(messages, model, stream=False, step="specify_file_paths")

    content = result["choices"][0]["message"]["content"]
    files = _extract_file_paths(content)
    resolved = files if files else ["index.html", "style.css", "script.js"]
    if return_usage:
        return resolved, result.get("usage")
    return resolved


def plan(
    prompt: str,
    stream_handler: Optional[Callable[[bytes], None]] = None,
    model: str = get_model("plan"),
    extra_messages: List[Any] = [],
    return_usage: bool = False,
):
    messages = [
        {
            "role": "system",
            "content": f"""{SMOL_DEV_SYSTEM_PROMPT}

In response to the user's prompt, write a plan using GitHub Markdown syntax. Begin with a YAML description of the new files that will be created.
In this plan, please name and briefly describe the structure of code that will be generated, including, for each file we are generating, what variables they export, data schemas, id names of every DOM elements that javascript functions will use, message names, and function names.
Respond only with plans following the above schema.
                  """,
        },
        {
            "role": "user",
            "content": f""" the app prompt is: {prompt} """,
        },
        *extra_messages,
    ]

    response = _create_completion(messages, model, stream=True, step="plan")

    collected_content = []
    for line in response.iter_lines():
        if line:
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                try:
                    chunk = json.loads(line_str[6:])
                    if chunk.get("choices"):
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_content.append(content)
                            if stream_handler:
                                try:
                                    stream_handler(content.encode("utf-8"))
                                except Exception as err:
                                    logger.info(f"stream_handler error: {err}")
                except json.JSONDecodeError:
                    continue

    content = "".join(collected_content)
    if content.strip():
        if return_usage:
            return content, getattr(response, "usage", None)
        return content

    # Some providers may not emit token deltas in stream mode; recover via non-stream call.
    fallback = _create_completion(messages, model, stream=False, step="plan")
    fallback_content = fallback["choices"][0]["message"]["content"]
    if return_usage:
        return fallback_content, fallback.get("usage")
    return fallback_content


@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
async def generate_code(
    prompt: str,
    plan: str,
    current_file: str,
    stream_handler: Optional[Callable[[bytes], None]] = None,
    model: str = get_model("generate_code"),
    return_usage: bool = False,
) -> str:
    messages = [
        {
            "role": "system",
            "content": f"""{SMOL_DEV_SYSTEM_PROMPT}

In response to the user's prompt,
Please name and briefly describe the structure of the app we will generate, including, for each file we are generating, what variables they export, data schemas, id names of every DOM elements that javascript functions will use, message names, and function names.

We have broken up the program into per-file generation.
Now your job is to generate only the code for the file: {current_file}

only write valid code for the given filepath and file type, and return only the code.
do not add any other explanation, only return valid code for that file type.
                  """,
        },
        {
            "role": "user",
            "content": f""" the plan we have agreed on is: {plan} """,
        },
        {
            "role": "user",
            "content": f""" the app prompt is: {prompt} """,
        },
        {
            "role": "user",
            "content": f"""
Make sure to have consistent filenames if you reference other files we are also generating.

Remember that you must obey 3 things:
   - you are generating code for the file {current_file}
   - do not stray from the names of the files and the plan we have decided on
   - MOST IMPORTANT OF ALL - every line of code you generate must be valid code. Do not include code fences in your response, for example

Bad response (because it contains the code fence):
```javascript
console.log("hello world")
```

Good response (because it only contains the code):
console.log("hello world")

Begin generating the code now.

""",
        },
    ]

    response = _create_completion(messages, model, stream=True, step="generate_code")

    collected_content = []
    for line in response.iter_lines():
        if line:
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                try:
                    chunk = json.loads(line_str[6:])
                    if chunk.get("choices"):
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_content.append(content)
                            if stream_handler:
                                try:
                                    stream_handler(content.encode("utf-8"))
                                except Exception as err:
                                    logger.info(f"stream_handler error: {err}")
                except json.JSONDecodeError:
                    continue

    code_file = "".join(collected_content)
    if not code_file.strip():
        # Some providers may not emit token deltas in stream mode; recover via non-stream call.
        fallback = _create_completion(messages, model, stream=False, step="generate_code")
        code_file = fallback["choices"][0]["message"]["content"]
        fallback_usage = fallback.get("usage")
    else:
        fallback_usage = getattr(response, "usage", None)

    # Remove code fences if present
    pattern = r"```[\w\s]*\n([\s\S]*?)```"
    code_blocks = re.findall(pattern, code_file, re.MULTILINE)
    resolved_code = code_blocks[0] if code_blocks else code_file
    if return_usage:
        return resolved_code, fallback_usage
    return resolved_code


def generate_code_sync(
    prompt: str,
    plan: str,
    current_file: str,
    stream_handler: Optional[Callable[[bytes], None]] = None,
    model: str = get_model("generate_code"),
) -> str:
    return asyncio.run(generate_code(prompt, plan, current_file, stream_handler, model))
