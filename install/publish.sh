#!/usr/bin/env bash
#
# Datafye Agent - Installer Publisher
#
# Publishes versioned installer scripts and version file to downloads server.
# Run this as part of the Datafye release pipeline.
#
# Usage:
#   ./publish.sh <downloads_root> <version> [--dry-run]
#
# Example:
#   ./publish.sh /var/www/downloads 2.0.4
#   ./publish.sh /var/www/downloads 2.0.4 --dry-run
#
# Published files:
#   downloads_root/datafye/agent/<version>/install.sh
#   downloads_root/datafye/agent/<version>/upgrade-check.sh
#   downloads_root/datafye/agent/<version>/build-ami.sh
#   downloads_root/datafye/agent/latest/version.txt         (triggers auto-upgrades)
#   downloads_root/datafye/agent/latest -> <version>        (symlink)
#

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_PATH="datafye/agent"

info()  { echo "INFO: $*"; }
warn()  { echo "WARN: $*"; }
ok()    { echo "  ok: $*"; }
error() { echo "ERROR: $*" >&2; }

usage() {
    cat <<EOF
Datafye Agent Installer Publisher

Usage:
  publish.sh <downloads_root> <version> [--dry-run]

Arguments:
  downloads_root    Root directory for downloads (e.g., /var/www/downloads)
  version           Datafye platform version (e.g., 2.0.4)

Options:
  --dry-run         Show what would be done without making changes
  -h, --help        Show this help message

Published structure:
  <root>/datafye/agent/<version>/install.sh
  <root>/datafye/agent/<version>/upgrade-check.sh
  <root>/datafye/agent/latest/version.txt
  <root>/datafye/agent/latest/install.sh
  <root>/datafye/agent/latest/upgrade-check.sh
EOF
}

validate_version() {
    local version="$1"
    if [[ "$version" == "latest" ]]; then
        error "Cannot publish 'latest' as a version. Use a real version number."
        exit 1
    fi
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?(-[a-zA-Z0-9.-]+)?$ ]]; then
        error "Invalid version format: $version"
        error "Expected: X.Y.Z or X.Y.Z-suffix (e.g., 2.0.4, 2.0.4-SNAPSHOT)"
        exit 1
    fi
}

bake_version() {
    # Replace __VERSION__ placeholder with actual version in a script
    local version="$1"
    local input="$2"
    local output="$3"

    sed "s/__VERSION__/$version/g" "$input" > "$output"
    chmod +x "$output"
}

main() {
    local downloads_root=""
    local version=""
    local dry_run=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help) usage; exit 0 ;;
            --dry-run) dry_run=true; shift ;;
            -*)        error "Unknown option: $1"; exit 1 ;;
            *)
                if [[ -z "$downloads_root" ]]; then
                    downloads_root="$1"
                elif [[ -z "$version" ]]; then
                    version="$1"
                else
                    error "Too many arguments: $1"; exit 1
                fi
                shift
                ;;
        esac
    done

    if [[ -z "$downloads_root" ]] || [[ -z "$version" ]]; then
        error "Both downloads_root and version are required"
        usage
        exit 1
    fi

    validate_version "$version"

    # Check source files exist
    for f in install.sh upgrade-check.sh; do
        if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
            error "Source script not found: ${SCRIPT_DIR}/${f}"
            exit 1
        fi
    done

    if [[ ! -d "$downloads_root" ]]; then
        error "Downloads root not found: $downloads_root"
        exit 1
    fi

    local target_dir="$downloads_root/$PRODUCT_PATH/$version"
    local latest_dir="$downloads_root/$PRODUCT_PATH/latest"

    if [[ "$dry_run" == "true" ]]; then
        warn "DRY RUN MODE"
    fi

    info "Publishing Datafye Agent installer"
    info "  Version: $version"
    info "  Target:  $target_dir"
    echo ""

    if [[ "$dry_run" == "true" ]]; then
        info "[DRY RUN] Would create: $target_dir/"
        info "[DRY RUN] Would publish: install.sh (with version baked in)"
        info "[DRY RUN] Would publish: upgrade-check.sh"
        info "[DRY RUN] Would write:   $latest_dir/version.txt -> $version"
    else
        # Create version directory
        mkdir -p "$target_dir"

        # Bake version into install.sh (replace default VERSION="" with VERSION="2.0.4")
        local temp_dir="/tmp/datafye_agent_publish_$$"
        mkdir -p "$temp_dir"

        # install.sh — bake in the version as the default
        sed "s/^VERSION=\"\"/VERSION=\"${version}\"/" "${SCRIPT_DIR}/install.sh" > "$temp_dir/install.sh"
        chmod +x "$temp_dir/install.sh"
        cp "$temp_dir/install.sh" "$target_dir/install.sh"
        ok "Published: $target_dir/install.sh"

        # upgrade-check.sh — no version baking needed (it reads from /opt/datafye/agent/version)
        cp "${SCRIPT_DIR}/upgrade-check.sh" "$target_dir/upgrade-check.sh"
        chmod +x "$target_dir/upgrade-check.sh"
        ok "Published: $target_dir/upgrade-check.sh"

        rm -rf "$temp_dir"

        # Write version.txt (this is what triggers auto-upgrades)
        mkdir -p "$latest_dir"
        echo "$version" > "$latest_dir/version.txt"
        ok "Published: $latest_dir/version.txt -> $version"

        # Update latest symlink
        local base_dir="$downloads_root/$PRODUCT_PATH"
        if [[ -L "$base_dir/latest" ]]; then
            rm "$base_dir/latest"
        elif [[ -d "$base_dir/latest" ]]; then
            # latest is a real dir (has version.txt), keep it but also create version symlink
            : # version.txt already written above
        fi

        # Copy versioned scripts into latest/ too (so latest/install.sh works)
        cp "$target_dir/install.sh" "$latest_dir/install.sh"
        cp "$target_dir/upgrade-check.sh" "$latest_dir/upgrade-check.sh"
        ok "Updated: latest/ with v${version} scripts"
    fi

    echo ""
    ok "Published Datafye Agent installer v${version}"
    echo ""
    if [[ "$dry_run" != "true" ]]; then
        info "Installer URL:"
        info "  https://downloads.n5corp.com/$PRODUCT_PATH/$version/install.sh"
        info "  https://downloads.n5corp.com/$PRODUCT_PATH/latest/install.sh"
        echo ""
        info "Auto-upgrade will trigger within 5 minutes on all running instances."
    fi
}

main "$@"
