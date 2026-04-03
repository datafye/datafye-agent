#!/usr/bin/env bash
#
# Datafye Agent - Installer Publisher
#
# Publishes versioned installer scripts to downloads server.
# Run this as part of the Datafye release pipeline.
#
# Usage:
#   ./publish_installer.sh <downloads_root> <version> [--dry-run]
#
# Example:
#   ./publish_installer.sh /var/www/downloads 2.0.4
#   ./publish_installer.sh /var/www/downloads 2.0.4 --dry-run
#
# Published files:
#   downloads_root/datafye/agent/<version>/install.sh
#   downloads_root/datafye/agent/<version>/upgrade-check.sh
#   downloads_root/datafye/agent/latest -> <version>         (symlink)
#

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_PATH="datafye/agent"

# ── Output functions (no colors for build server) ────────────────
info()  { echo "INFO: $*"; }
warn()  { echo "WARN: $*"; }
ok()    { echo "✓ $*"; }
error() { echo "ERROR: $*" >&2; }

usage() {
    cat <<EOF
Datafye Agent Installer Publisher

Usage:
  publish_installer.sh <downloads_root> <version> [--dry-run]

Arguments:
  downloads_root    Root directory containing 'datafye' (e.g., /var/www/downloads)
  version           Version to publish (e.g., "2.0.4", "latest")

Options:
  --dry-run         Show what would be done without making changes
  -h, --help        Show this help message

Published structure:
  <root>/datafye/agent/<version>/install.sh
  <root>/datafye/agent/<version>/upgrade-check.sh
  <root>/datafye/agent/latest -> <version>   (symlink)
EOF
}

validate_version() {
    local version="$1"

    # Allow "latest" as a special version
    if [[ "$version" == "latest" ]]; then
        return 0
    fi

    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?(-[a-zA-Z0-9.-]+)?$ ]]; then
        error "Invalid version format: $version"
        error "Expected format: X.Y.Z, X.Y.Z-suffix, or X.Y-suffix (e.g., 2.0.4, 2.0.4-SNAPSHOT)"
        exit 1
    fi
}

check_prerequisites() {
    local downloads_root="$1"

    for f in install_template.sh upgrade-check.sh; do
        if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
            error "Source script not found: ${SCRIPT_DIR}/${f}"
            exit 1
        fi
    done

    if [[ ! -d "$downloads_root" ]]; then
        error "Downloads root directory not found: $downloads_root"
        exit 1
    fi

    mkdir -p "$downloads_root/$PRODUCT_PATH"
}

publish_version() {
    local downloads_root="$1"
    local version="$2"
    local dry_run="$3"

    local target_dir="$downloads_root/$PRODUCT_PATH/$version"

    info "Publishing version: $version"

    if [[ "$dry_run" == "true" ]]; then
        info "[DRY RUN] Would create directory: $target_dir"
        info "[DRY RUN] Would publish: install.sh (with version baked in)"
        info "[DRY RUN] Would publish: upgrade-check.sh"
    else
        mkdir -p "$target_dir"

        # Bake version into install.sh (replace default VERSION="")
        sed "s/^VERSION=\"\"/VERSION=\"${version}\"/" "${SCRIPT_DIR}/install_template.sh" > "$target_dir/install.sh"
        chmod +x "$target_dir/install.sh"
        ok "Published: $target_dir/install.sh"

        cp "${SCRIPT_DIR}/upgrade-check.sh" "$target_dir/upgrade-check.sh"
        chmod +x "$target_dir/upgrade-check.sh"
        ok "Published: $target_dir/upgrade-check.sh"
    fi
}

update_latest_link() {
    local downloads_root="$1"
    local version="$2"
    local dry_run="$3"

    if [[ "$version" == "latest" ]]; then
        info "Skipping latest link update (publishing 'latest' version)"
        return 0
    fi

    local base_dir="$downloads_root/$PRODUCT_PATH"
    local latest_link="$base_dir/latest"

    info "Updating latest symlink to point to: $version"

    if [[ "$dry_run" == "true" ]]; then
        info "[DRY RUN] Would update symlink: latest -> $version"
    else
        if [[ -L "$latest_link" ]]; then
            rm "$latest_link"
        fi

        (cd "$base_dir" && ln -sf "$version" "latest")
        ok "Updated: latest -> $version"
    fi
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
    check_prerequisites "$downloads_root"

    if [[ "$dry_run" == "true" ]]; then
        warn "DRY RUN MODE - No changes will be made"
    fi

    info "Publishing Datafye Agent installer"
    info "Version: $version"
    info "Target: $downloads_root/$PRODUCT_PATH"
    echo

    publish_version "$downloads_root" "$version" "$dry_run"

    if [[ "$version" != "latest" ]]; then
        update_latest_link "$downloads_root" "$version" "$dry_run"
    fi

    echo
    ok "Successfully published installer for version: $version"

    if [[ "$dry_run" != "true" ]]; then
        info "Installer available at:"
        info "  https://downloads.n5corp.com/$PRODUCT_PATH/$version/install.sh"

        if [[ "$version" != "latest" ]]; then
            info "  https://downloads.n5corp.com/$PRODUCT_PATH/latest/install.sh"
        fi
    fi
}

main "$@"
