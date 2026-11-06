# syntax=docker/dockerfile:1

# Stage 1: fetch the pinned agent runtime. The npm package ships a native
# binary per platform, so this stage needs Node but the final image does not.
FROM node:22-slim AS opencode
ARG OPENCODE_VERSION=1.18.35
RUN npm install --prefix /oc --no-audit --no-fund "opencode-ai@${OPENCODE_VERSION}" \
 && mkdir -p /usr/local/bin \
 && cp "$(find /oc/node_modules/opencode-ai/bin -maxdepth 1 -type f | head -1)" /usr/local/bin/opencode \
 && chmod +x /usr/local/bin/opencode

FROM python:3.12-slim

# No apt step: git is optional in the pipeline (a failed clone is reported
# per-submission, never fatal), and the slim image ships CA certificates.
# Keeping the image apt-free also keeps it buildable where deb.debian.org
# is unreachable.

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GRADING_PIPELINE_HOME=/data \
    GRADING_PIPELINE_WORKSPACE=/data/workspace \
    GRADING_PIPELINE_RENDERER=/app/apps/desktop/renderer \
    GRADING_PIPELINE_OPENCODE=/usr/local/bin/opencode \
    GRADING_PIPELINE_HOST=0.0.0.0 \
    GRADING_PIPELINE_PORT=8765

WORKDIR /app

# Runtime dependencies only. A non-editable install means no checkout is
# needed at runtime and the image works on a machine without the repo.
COPY pyproject.toml ./
COPY sidecar ./sidecar
RUN pip install --no-cache-dir .

COPY apps/desktop/renderer ./apps/desktop/renderer
COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode

RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=5).status == 200 else 1)"

CMD ["python", "sidecar/grading_sidecar.py"]
