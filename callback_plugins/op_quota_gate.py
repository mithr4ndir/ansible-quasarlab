#!/usr/bin/env python3
"""Abort a playbook before it starts if the 1Password quota is (nearly) gone.

Why this is a callback plugin and not another guard in the wrapper scripts:
run-proxmox.sh and run-security.sh already source lib/op-killswitch.sh, but
that only protects runs that go through a wrapper. On 2026-08-23 the account
cap was drained from 7 to 1000 in under an hour by `ansible-playbook` invoked
straight from a terminal (pts/3 and pts/7), which bypassed every wrapper guard
we had. A callback plugin loads for *every* playbook invocation, interactive
or scheduled, so the gate cannot be walked around by accident.

This also differs from the kill switch in direction. The kill switch is
reactive: it trips after 1Password has already answered "Too many requests",
by which point the window is pinned. This gate is proactive, and it is cheap
to be so because `op service-account ratelimit` does not itself count against
the quota (verified 2026-04-19).

Fails OPEN. If `op` is missing, times out, or emits something we cannot parse,
the playbook proceeds with a warning. This is a cost guard, not a security
control, and wrongly blocking every playbook in the lab is worse than the
overspend it prevents. It fails CLOSED only when it positively reads a
remaining value at or below the threshold.

Environment:
    OP_QUOTA_GATE_MIN_REMAINING  Block below this many remaining. Default 50.
    OP_QUOTA_GATE_BYPASS=1       Skip the check entirely (documented escape
                                 hatch for recovery work that must run while
                                 the quota is down).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: op_quota_gate
    type: aggregate
    short_description: Abort playbooks when the 1Password quota is exhausted
    description:
      - Runs `op service-account ratelimit` before the first play and aborts
        if the account read_write quota is at or below a threshold.
    requirements:
      - the `op` CLI on PATH
"""

DEFAULT_MIN_REMAINING = 50
OP_TIMEOUT_SECONDS = 20

# The row we care about. `op` emits a fixed-width table, not JSON (as of
# 2026-08), so match on the leading two columns rather than splitting blindly.
ACCOUNT_ROW = re.compile(
    r"^\s*account\s+read_write\s+(\d+)\s+(\d+)\s+(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "op_quota_gate"
    # Auto-load; do not require an entry in callbacks_enabled, otherwise an
    # interactive run could silently skip the gate by using a different cfg.
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self) -> None:
        super().__init__()
        self._checked = False

    def v2_playbook_on_start(self, playbook) -> None:
        # Guard against multiple playbooks in one process re-running the check.
        if self._checked:
            return
        self._checked = True

        if os.environ.get("OP_QUOTA_GATE_BYPASS") == "1":
            self._display.warning(
                "1P quota gate BYPASSED via OP_QUOTA_GATE_BYPASS=1. "
                "Watch the account cap manually."
            )
            return

        threshold = self._threshold()
        remaining = self._remaining()

        if remaining is None:
            # Fail open, loudly. See module docstring.
            self._display.warning(
                "1P quota gate could not read the account quota; proceeding "
                "anyway. If this run is large, check `op service-account "
                "ratelimit` by hand first."
            )
            return

        if remaining <= threshold:
            self._abort(remaining, threshold)

        if remaining <= threshold * 4:
            self._display.warning(
                f"1P account quota is getting low: {remaining} remaining "
                f"(gate blocks at {threshold})."
            )

    def _abort(self, remaining: int, threshold: int) -> None:
        """Stop the run before any task executes.

        SystemExit rather than AnsibleError on purpose. Ansible's callback
        dispatcher wraps every hook in `except Exception`, so an AnsibleError
        raised here is logged and then swallowed, and the playbook runs anyway
        (verified 2026-08-24). SystemExit derives from BaseException, so it
        escapes that handler and actually stops the process. Nothing has run
        at this point, so there is no partial state to unwind.
        """
        self._display.error(
            f"1Password account read_write quota is down to {remaining} "
            f"(gate threshold {threshold}). Refusing to start this playbook: "
            f"a full run can spend more than that and would keep the 24h "
            f"window pinned."
        )
        self._display.error("  Check status : op service-account ratelimit")
        self._display.error("  Run anyway   : OP_QUOTA_GATE_BYPASS=1 ansible-playbook ...")
        self._display.error("  Tune the gate: OP_QUOTA_GATE_MIN_REMAINING=<n>")
        raise SystemExit(2)

    def _threshold(self) -> int:
        raw = os.environ.get("OP_QUOTA_GATE_MIN_REMAINING")
        if raw is None:
            return DEFAULT_MIN_REMAINING
        try:
            value = int(raw)
        except ValueError:
            self._display.warning(
                f"OP_QUOTA_GATE_MIN_REMAINING={raw!r} is not an integer; "
                f"using default {DEFAULT_MIN_REMAINING}."
            )
            return DEFAULT_MIN_REMAINING
        # A negative threshold would disable the gate silently, which is worse
        # than the documented bypass env var.
        return max(value, 0)

    def _remaining(self) -> Optional[int]:
        """Return account read_write remaining, or None if undeterminable."""
        op_path = shutil.which("op")
        if op_path is None:
            return None
        try:
            proc = subprocess.run(
                [op_path, "service-account", "ratelimit"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=OP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return None

        if proc.returncode != 0:
            return None
        return parse_account_remaining(proc.stdout)


def parse_account_remaining(output: str) -> Optional[int]:
    """Extract REMAINING from the `account read_write` row.

    Kept module level and free of ansible imports so it is unit testable.
    """
    match = ACCOUNT_ROW.search(output or "")
    if match is None:
        return None
    limit, used, remaining = (int(g) for g in match.groups())
    # Sanity check the row rather than trusting column order blindly; if the
    # CLI ever reorders columns this catches it instead of silently gating on
    # the wrong number.
    if used + remaining != limit:
        return None
    return remaining
