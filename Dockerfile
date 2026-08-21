FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.13.7-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.7-slim-bookworm
RUN groupadd --gid 65532 nonroot && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["pr-html-preview"]
