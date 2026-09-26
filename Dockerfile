# Linux-safe unit tests in a clean environment. Windows integration tests cannot run here;
# GitHub CI on windows-latest is the source of truth for those.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_FROZEN=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --group dev --no-install-project

COPY steerai ./steerai
COPY tests ./tests
RUN uv sync --group dev

CMD ["uv", "run", "pytest"]
