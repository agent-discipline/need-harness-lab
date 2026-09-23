FROM eclipse-temurin:21-jdk-jammy AS jdk
FROM node:22-bookworm-slim
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates bash unzip && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@0.155.1 && codex --version
RUN mkdir -p /home/node/.codex && chown node:node /home/node/.codex
USER node
WORKDIR /workspace
