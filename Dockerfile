# ============================================================================
# Medical Nudging Agent - Dockerfile (ARM64)
# ============================================================================
# This Dockerfile builds the Medical Nudging agent container for deployment
# to Amazon Bedrock AgentCore. Uses ARM64 architecture for optimal performance.
#
# Build: docker build --platform linux/arm64 -t medical-nudging-agent .
# Run:   docker run -p 8080:8080 medical-nudging-agent

# Python 3.11 from Amazon ECR Public (ARM64 compatible); uv installed from PyPI
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Set working directory
WORKDIR /app

# Install system dependencies
# - libpq-dev: PostgreSQL client library for psycopg2
# - poppler-utils: PDF processing for pdfplumber
# - libxml2-dev, libxslt-dev: XML processing for lxml/CCDA parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    poppler-utils \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.10.12

# Copy dependency files first for better caching
COPY pyproject.toml uv.lock* ./

# Copy source code
COPY src/ ./src/

# Copy prompts directory
COPY prompts/ ./prompts/

# Copy guidelines catalog and summaries (for list_guidelines and summaries search mode)
COPY guidelines/catalog.json ./guidelines/catalog.json
COPY guidelines/summaries/ ./guidelines/summaries/

# Copy README.md (required by pyproject.toml)
COPY README.md ./

# Copy agent entry point
COPY agent.py ./

# Create non-root user for security and set ownership BEFORE installing deps
RUN useradd -m -u 1000 agent && \
    chown -R agent:agent /app

# Switch to non-root user
USER agent

# Install dependencies using uv (as non-root user so .venv is owned by agent)
# --frozen: Use exact versions from lockfile
# --no-cache: Don't cache packages (smaller image)
# --no-dev: Skip development dependencies
RUN uv sync --frozen --no-cache --no-dev 2>/dev/null || uv sync --no-cache --no-dev

# Expose AgentCore required port
EXPOSE 8080

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

# Run the FastAPI application with uvicorn using OpenTelemetry auto-instrumentation
# opentelemetry-instrument: Enables automatic OTEL tracing for all agent operations
# --host 0.0.0.0: Listen on all interfaces (required for container)
# --port 8080: AgentCore required port
# --workers 1: Single worker (AgentCore manages scaling)
#
# The OTEL auto-instrumentation captures:
# - Agent invocations and model calls
# - Tool executions with timing
# - HTTP requests and responses
# Traces are automatically sent to CloudWatch via AgentCore's built-in OTEL pipeline
CMD ["uv", "run", "opentelemetry-instrument", "uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
