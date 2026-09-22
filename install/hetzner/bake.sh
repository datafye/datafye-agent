#!/bin/bash
#
# Datafye agent image bake for Hetzner (DAT-280) -- the Hetzner counterpart of packer/agent-hosted.pkr.hcl.
#
# Run by the Rumi provisioner's bakeImage (nvx-rumi-cloud, RUMI-433), which boots a throwaway server FROM the
# project's Rumi service image, copies this directory to /tmp/bake, runs this script as root (detached, with its
# output streamed back), and snapshots the result as role `datafye-agent`. Baking from the service image is not
# optional: it carries the rumi user the bastion logs in as to register the box's DNS.
#
#   rumi cloud hetzner bake-image -K <key> -o datafye-agent -d install/hetzner \
#       -c 'AGENT_VERSION=<v> /tmp/bake/bake.sh' -v <v>
#
# Uses the PUBLISHED installer for a release version, which needs no GitHub token (only SNAPSHOT installs do).
# Hetzner snapshots belong to ONE project, so run it once per project. There is no separate data volume on
# Hetzner: the server type fixes the disk, and everything lives on the root disk.

set -euo pipefail

if [ -z "${AGENT_VERSION:-}" ]; then
    echo "AGENT_VERSION must be set to a published Datafye agent release (e.g. AGENT_VERSION=2.0.52)" >&2
    exit 1
fi
case "$AGENT_VERSION" in
    *SNAPSHOT*) echo "A SNAPSHOT needs the private repos; bake a published release instead" >&2; exit 1 ;;
esac

cloud-init status --wait || true

if [ "${OS_UPGRADE:-false}" = "true" ]; then
    echo "Upgrading the base OS packages..."
    dnf upgrade -y
fi

echo "Downloading the published installer for v${AGENT_VERSION}..."
curl -fsSL "https://downloads.n5corp.com/datafye/agent/${AGENT_VERSION}/install.sh" -o /var/tmp/install.sh
chmod +x /var/tmp/install.sh

echo "Running the installer (--mode hosted --ami-cleanup)..."
/var/tmp/install.sh --mode hosted --ami-cleanup --version "${AGENT_VERSION}"
rm -f /var/tmp/install.sh

echo "Datafye agent v${AGENT_VERSION} baked."
