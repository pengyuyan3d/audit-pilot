# syntax=docker/dockerfile:1
# 多阶段构建 + 非 root 用户 + 健康检查（生产级容器化交付模板）

FROM python:3.13-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml ./
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python \
      fastapi "uvicorn[standard]" pydantic python-dotenv python-multipart \
      httpx openai openpyxl pypdf jieba rank-bm25 langgraph

FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd -r audit && useradd -r -g audit audit
COPY --from=builder /opt/venv /opt/venv
COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts
COPY eval ./eval
COPY sample_data ./sample_data
RUN mkdir -p /app/data && chown -R audit:audit /app

USER audit
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
