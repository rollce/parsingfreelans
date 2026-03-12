FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV PYTHONPATH=/app/src

CMD ["bash", "-lc", "python -m uvicorn freelans_bot.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
