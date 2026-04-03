#!/usr/bin/env python3
with open(r'c:\Agents\developer\smol_dev\prompts.py', 'r') as f:
    content = f.read()

old = """    loop = asyncio.get_event_loop()
    return loop.run_until_complete(generate_code(prompt, plan, current_file, stream_handler, model))"""

new = """    return asyncio.run(generate_code(prompt, plan, current_file, stream_handler, model))"""

content = content.replace(old, new)

with open(r'c:\Agents\developer\smol_dev\prompts.py', 'w') as f:
    f.write(content)

print("✅ Fixed asyncio issue")
