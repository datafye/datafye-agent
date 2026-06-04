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
Agent state-root resolution.

All of the agent's per-user, writable, on-disk state lives under a single
root directory so it can be relocated wholesale with one environment
variable — `DATAFYE_AGENT_STATE_DIR`. This is what keeps a local test run
from polluting the real `~/.datafye/agent` (point it at a scratch dir) and
keeps a deployed instance's state in one predictable place.

What lives under the state root:
  - credentials.bin        (encrypted credentials store)
  - broker_user.json       (legacy ConnectTrade user creds, migrated on load)
  - conversations/ | strategies/   (the chat/strategy store)
  - plugins/user/          (writable user-global skill plugin)

Each consumer keeps its own narrower override env var (e.g.
`DATAFYE_AGENT_CREDENTIALS_FILE`) which still wins when set — the state root
is just the default base those fall back to. The default root is the
historical `~/.datafye/agent`, so existing deployments are unaffected.
"""

from __future__ import annotations

import os

STATE_DIR = os.path.abspath(
    os.path.expanduser(
        os.environ.get("DATAFYE_AGENT_STATE_DIR", "~/.datafye/agent")
    )
)


def state_path(*parts: str) -> str:
    """Join `parts` onto the agent state root."""
    return os.path.join(STATE_DIR, *parts)
