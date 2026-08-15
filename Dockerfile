# Mission Control service image for AWS Fargate.
#
# The service and its Claude Agent SDK worker run in one container. The SDK spawns
# the Claude Code CLI as a subprocess, so the image carries Node + the CLI alongside
# Python; git provides the worktree isolation and the clone/push of target repos.
# The worker reaches Claude through Bedrock (CLAUDE_CODE_USE_BEDROCK=1 + the Fargate
# task role's AWS credentials), so no Anthropic API key is baked in.
#
# Build for the Fargate CPU architecture (arm64 to match Homebase's other services):
#   docker build --platform linux/arm64 -t <ecr>:<tag> .

FROM python:3.12-slim

# git (worktrees + target-repo clone/push), libpq5 (psycopg3 needs libpq at
# runtime for the Postgres ledger), and Node (the Claude Code CLI the SDK spawns).
# Node via NodeSource; the CLI installed globally onto PATH as `claude`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl gnupg libpq5 \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Fargate binds all interfaces (the port is VPC-internal only, and MC_API_TOKEN is
# required). Real SDK worker, routed to Claude via Bedrock.
ENV MC_SERVICE_HOST=0.0.0.0 \
    MC_SERVICE_PORT=8000 \
    MC_SERVICE_SDK=1 \
    CLAUDE_CODE_USE_BEDROCK=1
EXPOSE 8000
# The entrypoint wires git auth from GITHUB_TOKEN (env-based, never on disk), then
# runs the service.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "mission_control.service"]
