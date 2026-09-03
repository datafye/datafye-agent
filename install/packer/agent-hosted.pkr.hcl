# Packer template — bakes a Datafye Agent AMI in "hosted" mode.
#
# Hosted mode means: agent runs natively on the host with no nginx/SSL
# (the Rumi cloud's jump server handles the wildcard SSL + reverse proxy).
# Mirrors how the jump-server AMI itself is structured.
#
# Source AMI: the Rumi Service Worker AMI (Amazon Linux 2023 base with the
# Rumi worker scaffolding pre-installed — Java, the rumi user with sudo,
# the standard layout the rest of the cloud expects). The agent installer
# layers Python + datafye-agent on top.
#
# Output AMI name: datafye-agent-amzn2023-x86_64-v<version>. One AMI per
# agent version. Re-bakes of the same version replace the old AMI
# (force_deregister = true), so failed builds can be retried cleanly.
# To preserve old AMIs, bump the version.
#
# Local usage (for spot-checks before wiring TeamCity):
#   packer init  agent-hosted.pkr.hcl
#   packer build \
#     -var agent_version=2.0.5 \
#     -var github_token=ghp_xxx        \  # only needed for SNAPSHOT installs
#     agent-hosted.pkr.hcl
#
# CI usage:
#   The TeamCity build config Products_Datafye_Agent_Main_AmiBake passes
#   -var agent_version=%build.number% (matching the snapshot/release
#   build counter convention used by Datafye Core + Samples).

packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1.3"
    }
  }
}

# ── Inputs ───────────────────────────────────────────────────────────────

variable "agent_version" {
  type        = string
  description = "Agent version baked into the AMI (becomes part of the AMI name and the installer's --version)."
}

variable "github_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "GitHub PAT with read access to the private datafye-docs repo. Required for SNAPSHOT installs; ignored otherwise."
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "volume_size_gb" {
  type        = number
  default     = 8
  description = "OS root (/dev/xvda) size (GB), bake + AMI. Everything that grows -- Docker's data-root (which holds the foundry images AND the history service's tick/OHLC store), /opt/datafye, and the agent workspace -- lives on the data volume (data_volume_size_gb); root holds only the OS, the base AMI's Java, and dnf's caches. ⚠️ 8 is NOT a free choice: accounts calls AwsProvisioner.launchServiceInstance's overload that takes no rootVolumeSize, and that path computes `bootVolumeSize = rumiVolumeSize > 0 ? 8 : 32`. The moment accounts asks for a data volume it asks for an 8 GiB root, and EC2 rejects a root smaller than the AMI's snapshot -- so baking anything above 8 makes every provision fail at RunInstances. The provisioner additionally rejects an explicit rootVolumeSize <= 8, so there is no larger-root escape without a provisioner change. (DAT-178)"
}

variable "data_volume_size_gb" {
  type        = number
  default     = 64
  description = "The /home/rumi data volume (/dev/sdb) size (GB), bake + AMI. Holds Docker's data-root -- and therefore the foundry's rumi-<ds>-history-shared volume, i.e. every tick and OHLC log the history service writes -- plus /opt/datafye and /home/datafye via bind mounts. Matches Rumi Support's bake. Sized for the realistic case (a few hundred symbols of history is single-digit GB) rather than the pathological one: a year of minute bars for the full 8,502-symbol NYSE+NASDAQ universe is ~178GB, which is a resize, not a default. The base Rumi Worker AMI ships /dev/sdb at only 1GB; this resizes it. ⚠️ datafye.accounts.aws.rumi.volume.size must be >= this -- EC2 rejects a volume smaller than the AMI's snapshot. (DAT-178)"
}

variable "source_ami" {
  type        = string
  default     = "ami-007009ba912f34d31"   # RUMI_SERVICE_WORKER_AMI_V1 (per AwsProvisioner.java)
  description = "Source AMI to bake on top of. Defaults to the Rumi Service Worker AMI v1; bump when AwsProvisioner advances RUMI_SERVICE_WORKER_AMI_LATEST."
}

variable "agent_branch" {
  type        = string
  default     = "2.0"
  description = "Branch of the datafye-agent repo to clone for the bake. Defaults to 2.0 (the active development branch); GitHub's default branch (main) is currently a stale pre-2.0 snapshot."
}

# ── Source ───────────────────────────────────────────────────────────────

source "amazon-ebs" "agent_hosted" {
  region        = var.aws_region
  instance_type = var.instance_type
  source_ami    = var.source_ami
  ssh_username  = "rumi"    # Rumi worker AMIs ship with a 'rumi' user that has passwordless sudo

  ami_name        = "datafye-agent-amzn2023-x86_64-v${var.agent_version}"
  ami_description = "Datafye Agent v${var.agent_version} for Rumi cloud sandbox (hosted mode). Source: ${var.source_ami}."

  # Replace any existing AMI with the same name so failed builds can be
  # retried without manual deregistration. To preserve historical AMIs,
  # bump the version (each version produces a uniquely-named AMI).
  force_deregister      = true
  force_delete_snapshot = true

  # Bake-time disk. Two volumes, matching the runtime layout (DAT-178): a small
  # OS root, and the data volume the base AMI mounts at /home/rumi. The bake
  # needs the data volume too -- the installer stages the Datafye CLI tarball
  # and relocates Docker's data-root there, so baking on a single volume would
  # produce an AMI whose layout differs from every instance launched from it.
  launch_block_device_mappings {
    device_name = "/dev/xvda"
    volume_size = var.volume_size_gb
    volume_type = "gp3"
    delete_on_termination = true
  }
  launch_block_device_mappings {
    device_name = "/dev/sdb"
    volume_size = var.data_volume_size_gb
    volume_type = "gp3"
    delete_on_termination = true
  }

  # Runtime disk: instances launched from this AMI get the same two volumes.
  # Without these the sizes reset to the source AMI's defaults (8GB root, 1GB
  # /dev/sdb) on every launch. delete_on_termination=true on /dev/sdb also
  # fixes the base AMI's leak -- it ships /dev/sdb with delete_on_termination
  # unset, which orphans a data volume on every terminate.
  ami_block_device_mappings {
    device_name = "/dev/xvda"
    volume_size = var.volume_size_gb
    volume_type = "gp3"
    delete_on_termination = true
  }
  ami_block_device_mappings {
    device_name = "/dev/sdb"
    volume_size = var.data_volume_size_gb
    volume_type = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name         = "datafye-agent-amzn2023-x86_64-v${var.agent_version}"
    AgentVersion = var.agent_version
    AgentMode    = "hosted"
    SourceAmi    = var.source_ami
    BuiltBy      = "Datafye TeamCity"
  }
}

# ── Build ────────────────────────────────────────────────────────────────

build {
  sources = ["source.amazon-ebs.agent_hosted"]

  provisioner "shell" {
    # github_token is required: the datafye-agent and datafye-docs repos are
    # both private. Token is passed as an env var to the inline shell (rather
    # than being inlined into a clone URL on the command line) so it doesn't
    # land in process listings or shell history. Packer marks the variable
    # sensitive=true so it's redacted from build logs.
    environment_vars = [
      "GITHUB_TOKEN=${var.github_token}",
      "AGENT_VERSION=${var.agent_version}",
      "AGENT_BRANCH=${var.agent_branch}",
      # Redirect mktemp/tar staging off the tmpfs-backed /tmp (capped at
      # ~50% of RAM on AL2023, so ~1GB on a t3.small) AND off the now-small
      # root, onto the data volume. The Datafye CLI distribution tarball plus
      # its extracted contents together don't fit in 1GB. Created and grown
      # by the inline steps below before anything uses it. (DAT-178)
      "TMPDIR=/home/rumi/tmp",
    ]
    inline = [
      "set -e",
      "if [ -z \"$GITHUB_TOKEN\" ]; then echo 'ERROR: GITHUB_TOKEN is required (datafye-agent and datafye-docs are private)'; exit 1; fi",
      "echo 'Waiting for cloud-init to finish (Rumi worker AMIs may run on-boot setup)...'",
      "sudo cloud-init status --wait || true",
      # Grow the root partition + filesystem to fill the launch_block_device_mappings
      # volume. The Rumi Worker AMI's cloud-init doesn't do this automatically, so
      # without an explicit grow the agent installer (Java + Docker + Datafye CLI
      # extract + agent code) runs out of space at ~8GB of the 32GB volume.
      "echo 'Pre-grow disk usage:'; df -h /",
      "sudo growpart /dev/xvda 1 || echo '(growpart no-op — partition already at max)'",
      "sudo xfs_growfs / 2>/dev/null || sudo resize2fs /dev/root 2>/dev/null || sudo resize2fs /dev/xvda1 2>/dev/null || echo '(filesystem already at max)'",
      "echo 'Post-grow disk usage:'; df -h /",
      # Grow the /home/rumi data volume (/dev/sdb) to fill data_volume_size_gb.
      # The base AMI ships it at 1GB, and the installer puts Docker's data-root,
      # /opt/datafye and /home/datafye there, so it must be grown BEFORE the
      # installer runs. It is a raw filesystem directly on the device (no
      # partition table) and may be xfs or ext4, so try the mount-point xfs grow
      # then the device resize2fs under both the nvme and legacy names.
      "echo 'Pre-grow data volume:'; df -h /home/rumi || echo '(no data volume mounted)'",
      "sudo xfs_growfs /home/rumi 2>/dev/null || sudo resize2fs /dev/nvme1n1 2>/dev/null || sudo resize2fs /dev/sdb 2>/dev/null || echo '(data filesystem already at max)'",
      "echo 'Post-grow data volume:'; df -h /home/rumi || true",
      "sudo mkdir -p /home/rumi/tmp && sudo chmod 1777 /home/rumi/tmp",
      "echo 'Installing git...'",
      "sudo dnf install -y git",
      "echo \"Cloning datafye-agent branch $AGENT_BRANCH (private; using token)...\"",
      "git clone --depth 1 -b \"$AGENT_BRANCH\" \"https://x-access-token:$${GITHUB_TOKEN}@github.com/datafye/datafye-agent.git\" /tmp/datafye-agent",
      "cd /tmp/datafye-agent/install",
      "echo \"Running install_template.sh --mode hosted --ami-cleanup --version $AGENT_VERSION...\"",
      # --agent-source seeds /opt/datafye/agent/app from /tmp/datafye-agent
      # so the installer skips a github.com clone that would need a tag the
      # TC VCS labeling step hasn't pushed yet. The installer rewrites origin
      # to the canonical AGENT_REPO so the token-embedded URL doesn't end up
      # in the AMI.
      "sudo --preserve-env=GITHUB_TOKEN,TMPDIR ./install_template.sh --mode hosted --ami-cleanup --version \"$AGENT_VERSION\" --github-token \"$GITHUB_TOKEN\" --agent-source /tmp/datafye-agent",
      "echo 'Scrubbing /tmp/datafye-agent (its .git/config contains the token-embedded clone URL)...'",
      "sudo rm -rf /tmp/datafye-agent"
    ]
  }

  # Writes the resulting AMI ID to manifest.json so TeamCity can parse it
  # and publish (e.g.) downloads.n5corp.com/datafye/agent/<version>/ami-id.txt.
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
