# The txrisk image: the API, the scoring worker, the migration job and the seed and produce
# commands, each a `txrisk` command.

FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so code changes reuse their layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# LightGBM needs OpenMP at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home txrisk
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER txrisk
EXPOSE 8000
CMD ["txrisk", "serve", "--host", "0.0.0.0", "--port", "8000"]
