"""Operator SSH keys must be managed, not left to cloud-init.

cloud-init seeds authorized_keys once, at build time, so a VM built before a
key existed never receives it and no amount of re-running the playbooks fixes
that. On 2026-09-19 uptime-kuma, musicbot and jellyfin were all missing the
ed25519 key while the k8s nodes had it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "roles" / "common" / "vm_baseline"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

KEY_TYPES = ("ssh-rsa", "ssh-ed25519")


def key_task() -> dict:
    for t in yaml.safe_load(TASKS.read_text()):
        if "authorized_key" in str(t):
            return t
    raise AssertionError("vm_baseline has no authorized_key task")


class AuthorizedKeyTests(unittest.TestCase):
    def test_baseline_manages_authorized_keys(self):
        self.assertIn("ansible.posix.authorized_key", key_task())

    def test_both_key_types_are_configured(self):
        keys = yaml.safe_load(DEFAULTS.read_text())["vm_baseline_authorized_keys"]
        self.assertGreaterEqual(len(keys), 2)
        for kind in KEY_TYPES:
            self.assertTrue(
                any(k.startswith(kind) for k in keys),
                f"no {kind} key configured; a host missing it stays locked out",
            )

    def test_every_key_is_a_public_key(self):
        """A private key here would be a credential leak into git."""
        for k in yaml.safe_load(DEFAULTS.read_text())["vm_baseline_authorized_keys"]:
            self.assertTrue(k.startswith(KEY_TYPES), f"not a public key line: {k[:30]}")
            self.assertNotIn("PRIVATE KEY", k)

    def test_not_exclusive(self):
        """exclusive: true would delete keys added out of band, including
        cloud-init's, and could lock everyone out of a host."""
        defaults = yaml.safe_load(DEFAULTS.read_text())
        self.assertFalse(defaults["vm_baseline_authorized_keys_exclusive"])
        task = key_task()["ansible.posix.authorized_key"]
        self.assertEqual(task["exclusive"], "{{ vm_baseline_authorized_keys_exclusive }}")

    def test_keys_are_newline_joined(self):
        """The module takes many keys as one newline separated string; a list
        or a space joined string silently authorises nothing useful.

        The escape must still be an escape by the time Jinja sees it. In a
        double quoted YAML scalar, \\n becomes a real newline before Jinja
        parses the expression, which happens to work but only by accident.
        """
        key = key_task()["ansible.posix.authorized_key"]["key"]
        self.assertRegex(key, r"join\(\s*['\"]\\n['\"]\s*\)")
        self.assertNotIn("\n", key.replace("\\n", ""), "a real newline leaked into the template")


if __name__ == "__main__":
    unittest.main()
