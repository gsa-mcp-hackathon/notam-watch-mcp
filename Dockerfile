# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV PORT=8080

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

EXPOSE 8080

CMD ["python", "-m", "server.app"]

