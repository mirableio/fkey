FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Bind-mounted data/ and backups/ must be owned by this UID (see make deploy).
RUN useradd --system --uid 10001 --no-create-home fkey
USER fkey

CMD ["fkey", "--http", "--host", "0.0.0.0", "--port", "8000", "--public"]

