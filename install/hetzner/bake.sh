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
# 'latest' is a real published path, so it would fail ten minutes into the bake rather than here -- and
# whatever string is passed is also what the snapshot is labelled with, which imageMinVersion then compares.
if ! [[ "$AGENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "AGENT_VERSION must be an exact published release (X.Y.Z), not '$AGENT_VERSION'" >&2
    exit 1
fi

cloud-init status --wait || true

if [ "${OS_UPGRADE:-false}" = "true" ]; then
    echo "Upgrading the base OS packages..."
    dnf upgrade -y
fi

# The installer creates datafye as UID 1000, but on the service image that UID is already the rumi user, so
# `useradd -u 1000` fails. Nothing depends on the number, and the installer skips useradd for an existing user.
if ! id -u datafye &>/dev/null; then
    useradd -m -d /home/datafye -s /bin/bash datafye
fi

echo "Downloading the published installer for v${AGENT_VERSION}..."
curl -fsSL "https://downloads.n5corp.com/datafye/agent/${AGENT_VERSION}/install.sh" -o /var/tmp/install.sh
chmod +x /var/tmp/install.sh

echo "Running the installer (--mode hosted --ami-cleanup)..."
/var/tmp/install.sh --mode hosted --ami-cleanup --version "${AGENT_VERSION}"
rm -f /var/tmp/install.sh

# The provisioner snapshots the live disk, so quiesce everything that writes to it.
#
# crond first: the installer leaves an every-minute auto-upgrade job behind, and baking anything other than
# the current `latest` (a re-bake, or a deliberately older pin) means its first tick decides the box is out
# of date and runs the NEWEST installer over the disk being snapshotted -- restarting docker and rewriting
# the tree underneath us. The cron FILE stays; boxes launched from this image need it.
echo "Stopping cron and the container runtimes before the snapshot..."
systemctl stop crond 2>/dev/null || true
# Docker's containerd keeps an open bolt database; a snapshot taken mid-write yields a sandbox whose
# containers will not start. A runtime that refuses to stop is exactly the case this guards against, so it
# fails the bake rather than snapshotting it silently.
systemctl stop docker.socket docker containerd 2>/dev/null || true
for unit in docker containerd; do
    if systemctl is-active --quiet "$unit"; then
        echo "$unit is still running; refusing to snapshot a live container runtime" >&2
        exit 1
    fi
done
sync; sync

# These bake boxes have a history of files that were valid at bake time coming back empty on the snapshot,
# so assert what matters rather than ship a silently-broken image.
echo "Verifying the installed agent..."
if ! systemctl is-enabled --quiet datafye-agent.service; then
    echo "datafye-agent.service is not enabled; the image would boot without an agent" >&2
    exit 1
fi

# Every sibling Hetzner bake ends here: without it the snapshot carries this box's instance id and
# semaphores, and firstboot.sh never runs again on the boxes launched from it.
echo "Resetting cloud-init so instances launched from this image re-run their first boot..."
cloud-init clean --logs || true

echo "Datafye agent v${AGENT_VERSION} baked."
