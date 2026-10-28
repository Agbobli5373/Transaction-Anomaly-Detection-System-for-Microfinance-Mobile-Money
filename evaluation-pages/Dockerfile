# The evaluation pages (Streamlit): installs the locked `pages` dependency group alone, so the image
# holds no txrisk code, no database driver and no model. Build context is the repository root
# (`docker compose build evaluation-pages`), where pyproject.toml and uv.lock are.

FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --only-group pages

FROM python:3.12-slim
# Upgrade first, so any OS fix Debian has published is in the image (the Trivy scan fails on
# fixable High findings).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home pages
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY evaluation-pages ./
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 STREAMLIT_SERVER_PORT=8501
USER pages
EXPOSE 8501
CMD ["streamlit", "run", "app.py"]
