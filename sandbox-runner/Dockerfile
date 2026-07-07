# NOTE: intentionally NO `# syntax=` frontend pin. This Dockerfile uses only
# stock instructions (no heredocs / RUN --mount / COPY --link), so Docker's
# builtin BuildKit frontend handles it — and skipping the pin avoids a Docker
# Hub round-trip to resolve docker/dockerfile:1 that transiently hangs with
# DeadlineExceeded (ECA-64). If you ever add BuildKit-only syntax, restore the
# pin AND pre-pull the frontend + base image before building.
# ECA-64 hardened agent sandbox image (AC#1/AC#3).
# Day-one base: python:3.12-slim (git + a shell are required for the
# credential-helper clone, so distroless can't run the helper — DHI/distroless
# is a v2 residual). Credential-free, stateless: NO repo, NO token, NO host
# creds in any layer. Verify with `docker history` / `docker inspect`.

FROM python:3.12-slim

# --- OS deps: git (clone) + ca-certificates (TLS to GitHub/Bedrock via proxy) ---
# Pin nothing beyond the distro; keep the layer minimal and reproducible-ish.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# --- Non-secret env belt (container-sandbox.md) -------------------------------
# Bedrock backend, no non-essential traffic, no autoupdate (belt with the pin).
ENV CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/work

# --- Runner install ----------------------------------------------------------
# Copy only the subproject sources (the .dockerignore guarantees no .env/.git/
# creds enter the context). Installing the wheel pins claude-agent-sdk==0.2.91
# (bundles the Claude Code CLI) and nothing else.
WORKDIR /opt/sandbox-runner
COPY pyproject.toml README.md ./
COPY src ./src
RUN set -eux; \
    pip install --no-cache-dir .; \
    python -c "import sandbox_runner; import claude_agent_sdk"  # import smoke

# --- Non-root, ephemeral working area ----------------------------------------
# uid/gid 1000 non-root inside the container (the VM is the userns boundary on
# Mac; on Linux pair with the spawner's --user 1000:1000). /work is a tmpfs at
# runtime (read-only rootfs); create it so HOME resolves before the mount lands.
RUN set -eux; \
    groupadd --gid 1000 agent 2>/dev/null || true; \
    useradd --uid 1000 --gid 1000 --home-dir /work --no-create-home agent 2>/dev/null || true; \
    mkdir -p /work /job; \
    chown -R 1000:1000 /work /job

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0555 /usr/local/bin/entrypoint.sh

USER 1000:1000
WORKDIR /work

# entrypoint bridges /run/secrets/bedrock -> runner process env, then execs the
# runner as the current (uid 1000) user. The spawner supplies `--job-dir /job`.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--job-dir", "/job"]
