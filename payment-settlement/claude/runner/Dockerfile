FROM eclipse-temurin:21-jdk-jammy AS jdk
FROM node:22-bookworm-slim
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
# jq/flock(util-linux)/awk/realpath(coreutils) are required by the original harness hooks (arm 3).
RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates bash unzip jq util-linux procps && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.326 && claude --version
# v3-claude: no Codex. Arm 3's second reviewer is a fresh Claude session on the reviewer model. It runs inside a helper
# under its own account (fablereview, uid 2001) that alone holds the token; the PM's `fable-review` only sends the
# working directory and the prompt over /run/fable-review/socket. lab-arm3-start (arm 3 container start, as root with
# SETUID/SETGID/DAC_READ_SEARCH only) starts the helper and then the PM as node. Arms 1/2 start as node directly.
RUN useradd --uid 2001 --user-group --no-create-home --home-dir /home/fablereview --shell /usr/sbin/nologin fablereview \
 && mkdir -p /home/fablereview /run/fable-review /usr/local/lib/fable-review \
 && chown fablereview:fablereview /home/fablereview /run/fable-review && chmod 700 /home/fablereview && chmod 755 /run/fable-review
COPY fable-review.sh /usr/local/bin/fable-review
COPY lab-arm3-start.sh /usr/local/bin/lab-arm3-start
COPY fable_review.py /usr/local/lib/fable-review/fable_review.py
RUN chmod 755 /usr/local/bin/fable-review /usr/local/bin/lab-arm3-start && chmod 644 /usr/local/lib/fable-review/fable_review.py \
 && bash -n /usr/local/bin/fable-review && bash -n /usr/local/bin/lab-arm3-start \
 && python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' /usr/local/lib/fable-review/fable_review.py
# No self-update inside a frozen experiment image; subscription OAuth token is injected per run via env-file.
ENV DISABLE_AUTOUPDATER=1
# Hooks are wired as ~/.claude/hooks/*.sh and transcripts live under CLAUDE_CONFIG_DIR: both pinned, never implied.
ENV HOME=/home/node
ENV CLAUDE_CONFIG_DIR=/home/node/.claude
# The reviewer's config dir (mount point of the run's reviewer volume) belongs to the reviewer account and is closed to
# the PM's user: a new volume takes this owner and mode from the image.
RUN mkdir -p /home/node/.claude /home/node/.fable-review && chown node:node /home/node/.claude \
 && chown fablereview:fablereview /home/node/.fable-review && chmod 700 /home/node/.fable-review
USER node
WORKDIR /workspace
