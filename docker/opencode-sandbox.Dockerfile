# Isolated OpenCode ACP sandbox for Baton's Agents chat ("All permissions" mode).
# Build once on the host:
#   docker build -f docker/opencode-sandbox.Dockerfile -t baton-opencode-sandbox:latest .
#
# Baton mounts ONLY the selected conversation folder at /workspace and passes no
# host profile, Baton data, Docker socket, or provider credentials.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @opencode/cli \
    && npm cache clean --force \
    && useradd -m -s /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent
WORKDIR /workspace

CMD ["opencode", "acp"]
