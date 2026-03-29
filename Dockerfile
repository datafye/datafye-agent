# Datafye Agent Service
#
# Build with:
#   docker build --build-arg VERSION=2.0.4 -t datafye/datafye-agent:2.0.4 .
#
# The VERSION arg controls:
#   - Datafye CLI version installed
#   - datafye-docs repo tag checked out
#   - datafye-samples repo tag checked out
#   - Image label

FROM python:3.13-slim

ARG VERSION=2.0-SNAPSHOT
LABEL version="${VERSION}"
LABEL name="datafye-agent"

# ── System dependencies ───────────────────────────────────────────
# Java 17 (required by Datafye CLI), git (for repo ops), curl
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# ── Claude Code CLI (required by Agent SDK) ───────────────────────
RUN curl -fsSL https://claude.ai/install.sh | bash

# ── Datafye CLI ───────────────────────────────────────────────────
RUN curl -fsSL https://downloads.n5corp.com/datafye/cli/${VERSION}/install.sh | bash
ENV DATAFYE_CLI_PATH=/usr/local/opt/datafye/cli/${VERSION}/bin/datafye

# ── Datafye Docs (at release tag) ────────────────────────────────
RUN git clone --depth 1 --branch v${VERSION} \
    https://github.com/datafye/datafye-docs.git /opt/datafye/docs \
    || git clone --depth 1 https://github.com/datafye/datafye-docs.git /opt/datafye/docs

# ── Datafye Samples (at release tag) ─────────────────────────────
RUN git clone --depth 1 --branch v${VERSION} \
    https://github.com/datafye/datafye-samples.git /opt/datafye/samples \
    || git clone --depth 1 https://github.com/datafye/datafye-samples.git /opt/datafye/samples

# ── Python app ────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py prompt.py ./

# ── User and workspace ────────────────────────────────────────────
RUN useradd -u 1000 -m datafye && \
    mkdir -p /home/datafye/workspace && \
    chown -R datafye:datafye /home/datafye

# ── Environment defaults ─────────────────────────────────────────
ENV DATAFYE_AGENT_VERSION="${VERSION}"
ENV DATAFYE_AGENT_PORT="18780"
ENV DATAFYE_AGENT_WORKSPACE="/home/datafye/workspace"
ENV DATAFYE_DOCS_DIR="/opt/datafye/docs"
ENV DATAFYE_SAMPLES_DIR="/opt/datafye/samples"
ENV ANTHROPIC_API_KEY=""

EXPOSE 18780

USER datafye

CMD ["python", "main.py"]
