# Isolated Docker sandbox for Claude Code, Codex, and Gemini (agy) in /chat ("All permissions" mode).
#
# Build once on the host:
#   docker build -f docker/agents-sandbox.Dockerfile -t baton-agents-sandbox:latest .
#
# Then sign in once per CLI using its own isolated Docker named volume (Baton never
# reads, stores, or copies host credentials):
#   baton sandbox-login claude-code
#   baton sandbox-login codex
#   baton sandbox-login gemini
#
# At runtime Baton mounts ONLY the selected folder at /workspace and the single
# agent's own named auth volume, with --cap-drop=ALL, no-new-privileges, no host
# profile, no Baton data, no Docker socket, and no host secrets.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git jq python3 python3-venv ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force \
    && (id -u node >/dev/null 2>&1 && userdel -r node || true) \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /workspace /home/agent/.claude /home/agent/.codex /home/agent/.gemini \
    && chown -R agent:agent /workspace /home/agent

USER agent
ENV PATH="/home/agent/.local/bin:${PATH}"

# Install Google Antigravity CLI (agy) via the official Linux installer into /home/agent/.local/bin.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

WORKDIR /workspace
