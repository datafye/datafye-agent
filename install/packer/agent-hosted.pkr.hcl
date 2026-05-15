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

variable "source_ami" {
  type        = string
  default     = "ami-007009ba912f34d31"   # RUMI_SERVICE_WORKER_AMI_V1 (per AwsProvisioner.java)
  description = "Source AMI to bake on top of. Defaults to the Rumi Service Worker AMI v1; bump when AwsProvisioner advances RUMI_SERVICE_WORKER_AMI_LATEST."
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
    inline = [
      "set -e",
      "echo 'Waiting for cloud-init to finish (Rumi worker AMIs may run on-boot setup)...'",
      "sudo cloud-init status --wait || true",
      "echo 'Cloning datafye-agent...'",
      "sudo dnf install -y git",
      "git clone --depth 1 https://github.com/datafye/datafye-agent.git /tmp/datafye-agent",
      "cd /tmp/datafye-agent/install",
      "echo 'Running install_template.sh --mode hosted --ami-cleanup --version ${var.agent_version}...'",
      "sudo ./install_template.sh --mode hosted --ami-cleanup --version ${var.agent_version} ${var.github_token != "" ? "--github-token ${var.github_token}" : ""}"
    ]
  }

  # Writes the resulting AMI ID to manifest.json so TeamCity can parse it
  # and publish (e.g.) downloads.n5corp.com/datafye/agent/<version>/ami-id.txt.
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
