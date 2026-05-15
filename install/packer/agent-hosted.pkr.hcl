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
  default     = 32
  description = "Boot volume size (GB) for both the bake instance AND the resulting AMI. Source AMI defaults to ~8GB which can't fit Java + Docker + Datafye CLI extract + agent code + samples; 32GB gives headroom for Docker images at runtime too."
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

  # Bake-time disk: temp instance gets volume_size_gb on /dev/xvda so the
  # agent installer (Java + Docker + Datafye CLI extract + agent code +
  # samples) doesn't run out of space during dnf install / tar extract.
  launch_block_device_mappings {
    device_name = "/dev/xvda"
    volume_size = var.volume_size_gb
    volume_type = "gp3"
    delete_on_termination = true
  }

  # Runtime disk: instances launched from this AMI also get volume_size_gb
  # so Docker images + workspace files have room. Without this, AMI volume
  # size resets to the source AMI's default on every launch.
  ami_block_device_mappings {
    device_name = "/dev/xvda"
    volume_size = var.volume_size_gb
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
      "echo 'Installing git...'",
      "sudo dnf install -y git",
      "echo \"Cloning datafye-agent branch $AGENT_BRANCH (private; using token)...\"",
      "git clone --depth 1 -b \"$AGENT_BRANCH\" \"https://x-access-token:$${GITHUB_TOKEN}@github.com/datafye/datafye-agent.git\" /tmp/datafye-agent",
      "cd /tmp/datafye-agent/install",
      "echo \"Running install_template.sh --mode hosted --ami-cleanup --version $AGENT_VERSION...\"",
      "sudo --preserve-env=GITHUB_TOKEN ./install_template.sh --mode hosted --ami-cleanup --version \"$AGENT_VERSION\" --github-token \"$GITHUB_TOKEN\"",
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
