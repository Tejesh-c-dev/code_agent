<!-- Quick note: one-line comment added as requested. -->
# Sandbox Setup

Pull required runtime images before first sandboxed execution:

```powershell
docker pull python:3.11-slim
docker pull node:18-slim
docker pull bash:5.2
```

Optional: build a custom Python sandbox image with common packages pre-installed:

```powershell
docker build -t dev-assistant-python-sandbox -f dev_assistant/sandbox/Dockerfile .
```

Environment overrides:

- `SANDBOX_MODE`: `docker`, `subprocess`, or `auto`
- `SANDBOX_TIMEOUT`: timeout in seconds
- `SANDBOX_MEMORY`: Docker memory limit (example: `256m`)
- `SANDBOX_NETWORK`: `true` to allow network, `false` to disable network
