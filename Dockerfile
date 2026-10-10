FROM python:3.12-slim AS build
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.8.4
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
RUN groupadd --gid 10001 karma && useradd --uid 10001 --gid karma --no-create-home karma \
    && mkdir /data && chown karma:karma /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=8790 DATABASE_PATH=/data/karma.db
USER 10001:10001
EXPOSE 8790
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=2)"
CMD ["python", "-m", "karma_x.app"]
