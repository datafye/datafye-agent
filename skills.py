# Copyright 2025 Datafye
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Skill plugin wiring for the Datafye Agent.

Skills come in three tiers:

  - System (predefined): shipped in the repo under <app>/plugins/datafye,
    so they are installed and upgraded with the agent itself. Root-owned on
    a deployed instance, hence read-only to the user. The model auto-invokes
    them when relevant AND the user can invoke them explicitly.

  - User-global: written by the agent into ~/.datafye/agent/plugins/user.
    Reusable across every strategy in this user's workspace.

  - User per-strategy (Phase 1): <strategy>/.claude/skills, loaded by the SDK
    via setting_sources=["project"] when the strategy folder is the cwd. That
    tier is owned by the strategy store, not this module.

Both plugin tiers here are handed to the Claude Agent SDK as local plugins,
which the SDK forwards to the CLI as --plugin-dir. Plugins are rebuilt per
chat turn, so a skill the agent authors mid-session is live on the next turn.

A "local plugin" is a directory containing .claude-plugin/plugin.json plus a
skills/ subtree of <name>/SKILL.md files (the format Anthropic's own
`claude plugin init` scaffolds).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent

# System (predefined) skills — ship with the app clone, read-only.
SYSTEM_PLUGIN_DIR = Path(
    os.environ.get(
        "DATAFYE_AGENT_SYSTEM_PLUGIN_DIR",
        str(_APP_DIR / "plugins" / "datafye"),
    )
)

# User-global skills — agent-writable, reusable across strategies.
USER_PLUGIN_DIR = Path(
    os.environ.get(
        "DATAFYE_AGENT_USER_PLUGIN_DIR",
        paths.state_path("plugins", "user"),
    )
)

_USER_PLUGIN_MANIFEST = {
    "$schema": "https://anthropic.com/claude-code/plugin.schema.json",
    "name": "datafye-user",
    "version": "0.1.0",
    "description": (
        "User-defined skills created in this Datafye workspace, reusable "
        "across strategies."
    ),
    "skills": ["./skills"],
}


def _manifest_path(plugin_dir: Path) -> Path:
    return plugin_dir / ".claude-plugin" / "plugin.json"


def ensure_user_plugin() -> None:
    """Create the writable user-skill plugin scaffold if it does not exist.

    The SDK can load a plugin with zero skills, so scaffolding an empty plugin
    up front gives the agent a stable place to author cross-strategy skills
    without a chicken-and-egg problem. Best-effort: a failure here just means
    no user-global skills until the directory can be created."""
    skills_dir = USER_PLUGIN_DIR / "skills"
    manifest = _manifest_path(USER_PLUGIN_DIR)
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            manifest.write_text(json.dumps(_USER_PLUGIN_MANIFEST, indent=2))
            logger.info("Scaffolded user skill plugin at %s", USER_PLUGIN_DIR)
    except OSError as e:
        logger.warning(
            "Could not scaffold user skill plugin at %s: %s", USER_PLUGIN_DIR, e
        )


def build_plugins() -> list[dict]:
    """Local plugins to hand the SDK: system (read-only) + user-global (writable).

    A directory is included only if its manifest exists, so a missing or
    not-yet-scaffolded plugin dir never breaks a chat turn."""
    plugins: list[dict] = []
    for path in (SYSTEM_PLUGIN_DIR, USER_PLUGIN_DIR):
        if _manifest_path(path).is_file():
            plugins.append({"type": "local", "path": str(path)})
        else:
            logger.warning("Skipping plugin dir without manifest: %s", path)
    return plugins
