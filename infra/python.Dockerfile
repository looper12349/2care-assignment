# One locked Python runtime reused by the three independently started services.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src:/app \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY services ./services
COPY scenarios ./scenarios
COPY ARCHITECTURE.html ./ARCHITECTURE.html

# The editable project keeps scenario/document paths relative to /app.
# Credentials are supplied only when containers start, never during this build.
RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 carepath \
    && useradd --uid 10001 --gid carepath --create-home carepath \
    && mkdir /data \
    && chown carepath:carepath /data

USER carepath
ENV CLINIC_AGENT_DATA_DIR=/data

# Compose supplies the distinct module and port for each service.
CMD ["python", "-m", "uvicorn", "services.conversation.app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
