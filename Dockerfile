FROM node:22-bookworm-slim AS model-clis

ARG CLAUDE_CODE_VERSION=2.1.234
ARG CODEX_CLI_VERSION=0.136.0
RUN npm install --global \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force

# Codex 0.136.0's Linux ARM64 optional package is present but its package
# metadata uses the generic package name, so the JS launcher cannot resolve it
# by name. Its documented local-vendor fallback is stable and explicit.
RUN ln -s node_modules/@openai/codex-linux-arm64/vendor \
    /usr/local/lib/node_modules/@openai/codex/vendor

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Steel Mission" \
      org.opencontainers.image.description="Steel Mission starter company and governed software delivery plane"

COPY --from=model-clis /usr/local/bin/node /usr/local/bin/node
COPY --from=model-clis /usr/local/bin/claude /usr/local/bin/claude
COPY --from=model-clis /usr/local/bin/codex /usr/local/bin/codex
COPY --from=model-clis /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 501 steelmission \
    && useradd --uid 501 --gid 501 --create-home --home-dir /home/steelmission steelmission \
    && install -d -o 501 -g 501 \
       /home/steelmission/.claude \
       /home/steelmission/.codex \
       /var/lib/steel-mission \
       /workspace

# Use the packaged native binary directly. The npm launcher resolves its own
# global symlink as /usr/local/bin and misses the sibling optional package.
RUN ln -sf \
    /usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex \
    /usr/local/bin/codex

WORKDIR /workspace
COPY --chown=501:501 . /workspace

ENV HOME=/home/steelmission \
    PATH=/home/steelmission/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STEEL_MISSION_HOST=0.0.0.0 \
    STEEL_MISSION_PORT=8765 \
    STEEL_MISSION_RUNTIME_PROFILE=dc13.local \
    STEEL_MISSION_COORDINATOR_ROLE=dc13.coordination-report \
    STEEL_MISSION_COORDINATOR_PROVIDER=glimmer \
    STEEL_MISSION_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    STEEL_MISSION_WORKER_CLAUDE_TOKEN_FILE=/run/secrets/claude-token \
    STEEL_MISSION_TASKS_DIR=/var/lib/steel-mission/tasks \
    STEEL_MISSION_LOGS_DIR=/var/lib/steel-mission/logs \
    STEEL_MISSION_JOBS_DIR=/var/lib/steel-mission/jobs \
    STEEL_MISSION_MISSIONS_DIR=/var/lib/steel-mission/missions \
    STEEL_MISSION_TEST_RESULTS_DIR=/var/lib/steel-mission/test-results \
    STEEL_MISSION_REPOS_DIR=/var/lib/steel-mission/repos \
    STEEL_MISSION_CONFIG_DIR=/var/lib/steel-mission/config \
    STEEL_MISSION_ORG_KNOWLEDGE_UPLOAD_DIR=/var/lib/steel-mission/org-knowledge-uploads

USER 501:501
EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read()"

CMD ["bin/steel-mission", "serve"]
