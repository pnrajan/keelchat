# keelchat runtime + built console, the Knowledge Service and the Analytics Service (same image, different command).
# EXTRAS picks the optional dependencies to install: "api" (default, the API server), "knowledge", or
# "analytics sandbox agentcore"; add "audit" (Iceberg archive) or "mem0" when those are turned on.
#   docker build --build-arg EXTRAS="knowledge" -t keelchat-knowledge .
#   docker run -p 8080:8080 --env-file .env keelchat
#   docker run -p 8081:8080 --env-file .env keelchat uvicorn keelchat.knowledge.api:create_app --factory --host 0.0.0.0 --port 8080
#   docker run -p 8082:8080 --env-file .env keelchat uvicorn keelchat.analytics.api:create_app --factory --host 0.0.0.0 --port 8080
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent
COPY frontend ./
RUN npm run build --silent

FROM python:3.12-slim
ARG EXTRAS="api"
# bubblewrap + libseccomp: the Analytics Service's local sandbox (KEELCHAT_SANDBOX=local, backend bubblewrap)
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap libseccomp2 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY examples/contrib_tool examples/contrib_tool
RUN uv sync --frozen --no-dev $(for e in $EXTRAS; do printf -- "--extra %s " "$e"; done) --no-install-project && rm -rf /root/.cache/uv
COPY keelchat keelchat
COPY examples examples
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev $(for e in $EXTRAS; do printf -- "--extra %s " "$e"; done) && rm -rf /bin/uv /bin/uvx /root/.cache/uv
# with the Iceberg audit archive, the dashboards' DuckDB reads it on S3: install its extensions now, not at runtime
ENV KEELCHAT_DUCKDB_EXTENSIONS=/opt/duckdb-extensions
RUN case " $EXTRAS " in *" audit "*) /app/.venv/bin/python -c "import duckdb, os; c = duckdb.connect(config={'extension_directory': os.environ['KEELCHAT_DUCKDB_EXTENSIONS']}); [c.install_extension(e) for e in ('iceberg', 'avro', 'httpfs', 'aws')]";; esac
COPY --from=ui /ui/dist frontend/dist
COPY deploy/entrypoint.sh /entrypoint.sh
# the app only writes to /data; /app stays root-owned and readable (chown -R /app would copy every installed file into a new layer)
RUN useradd -r -u 10001 app && mkdir -p /data && chown -R app /data && chmod +x /entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# starts as root only to take ownership of mounted volumes (e.g. /data), then drops to `app`
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "keelchat.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
