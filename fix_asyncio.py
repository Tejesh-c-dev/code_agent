import re

file_path = r"c:\Agents\developer\smol_dev\prompts.py"

with open(file_path, 'r') as f:
    content = f.read()

# Replace the async issue
old_code = """def generate_code_sync(prompt: str, plan: str, current_file: str,
                       stream_handler: Optional[Callable[Any, Any]] = None,
                       model: str = 'openai/gpt-4o-mini') -> str:
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(generate_code(prompt, plan, current_file, stream_handler, model))"""

new_code = """def generate_code_sync(prompt: str, plan: str, current_file: str,
                       stream_handler: Optional[Callable[Any, Any]] = None,
                       model: str = 'openai/gpt-4o-mini') -> str:
    return asyncio.run(generate_code(prompt, plan, current_file, stream_handler, model))"""

content = content.replace(old_code, new_code)

with open(file_path, 'w') as f:
    f.write(content)

print("Done!")
