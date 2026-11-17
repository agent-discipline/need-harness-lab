FROM eclipse-temurin:21-jdk-jammy AS jdk
FROM node:22-bookworm-slim
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
# jq/flock(util-linux)/awk/realpath(coreutils) are required by the original harness hooks (arm 3).
RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates bash unzip jq util-linux procps && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.280 @openai/codex@0.155.1 && claude --version && codex --version
# Decision D2: codex review calls keep their sessions (usage observability). Wrapper strips --ephemeral only.
RUN mv /usr/local/bin/codex /usr/local/bin/codex-real
COPY codex-wrapper.sh /usr/local/bin/codex
RUN chmod 755 /usr/local/bin/codex && codex --version
# No self-update inside a frozen experiment image; subscription OAuth token is injected per run via env-file.
ENV DISABLE_AUTOUPDATER=1
# Hooks are wired as ~/.claude/hooks/*.sh and transcripts live under CLAUDE_CONFIG_DIR: both pinned, never implied.
ENV HOME=/home/node
ENV CLAUDE_CONFIG_DIR=/home/node/.claude
RUN mkdir -p /home/node/.claude /home/node/.codex && chown node:node /home/node/.claude /home/node/.codex
USER node
WORKDIR /workspace
