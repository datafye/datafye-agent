#!/bin/bash
#
# Build the Datafye Agent Docker image.
#
# Usage:
#   ./build.sh <version>
#
# Example:
#   ./build.sh 2.0.4
#
# This builds an image tagged datafye/datafye-agent:<version> with:
#   - Datafye CLI v<version>
#   - datafye-docs at tag v<version>
#   - datafye-samples at tag v<version>
#   - Claude Code CLI (latest)
#   - Python 3.13 + FastAPI + Claude Agent SDK

set -e

VERSION=${1:?"Usage: ./build.sh <version>"}

echo "Building datafye-agent:${VERSION}"
echo "  CLI version: ${VERSION}"
echo "  Docs tag: v${VERSION}"
echo "  Samples tag: v${VERSION}"

docker build \
    --build-arg VERSION="${VERSION}" \
    -t "datafye/datafye-agent:${VERSION}" \
    -t "datafye/datafye-agent:latest" \
    .

echo ""
echo "Done. Image: datafye/datafye-agent:${VERSION}"
echo "Run with:"
echo "  docker run -d \\"
echo "    --name datafye-agent \\"
echo "    -p 18780:18780 \\"
echo "    -e ANTHROPIC_API_KEY=sk-ant-... \\"
echo "    -v /path/to/workspace:/home/datafye/workspace \\"
echo "    datafye/datafye-agent:${VERSION}"
