FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY migrations ./migrations
COPY openspec-templates ./openspec-templates

RUN useradd --system --uid 10001 sdd
USER sdd

ENV SDD_HOST=0.0.0.0 SDD_PORT=48080
EXPOSE 48080
CMD ["/app/.venv/bin/sdd", "serve"]
