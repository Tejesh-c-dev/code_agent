FROM python:3.11-slim

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
    requests \
    numpy \
    pandas \
    pydantic \
    pytest

CMD ["python", "--version"]
