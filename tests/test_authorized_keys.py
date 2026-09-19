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

    def test_no_key_has_a_mangled_comment(self):
        """Each entry must be exactly type, blob, comment.

        The ed25519 key was first copied verbatim off k8cluster1, where the
        line reads `... cwladino@outlook.com1~ssh-ed25519 AAAA... ` three
        times over: a stripped Home-key escape (ESC[1~) from an old paste.
        sshd accepts it, because everything past the blob is just the comment,
        so it authorises the right key and hides in plain sight. Copying host
        state into the repo copies its scars too.
        """
        for k in yaml.safe_load(DEFAULTS.read_text())["vm_baseline_authorized_keys"]:
            fields = k.split()
            self.assertEqual(
                len(fields), 3, f"expected type/blob/comment, got {len(fields)} fields"
            )
            self.assertNotIn(
                "ssh-", fields[2], f"key type embedded in the comment: {fields[2][:40]}"
            )
            self.assertNotIn("1~", k, "stripped terminal escape in the key line")

    def test_not_exclusive(self):
        """exclusive: true would delete keys added out of band, including
        cloud-init's, and could lock everyone out of a host."""
        defaults = yaml.safe_load(DEFAULTS.read_text())
        self.assertFalse(defaults["vm_baseline_authorized_keys_exclusive"])
        task = key_task()["ansible.posix.authorized_key"]
        self.assertEqual(task["exclusive"], "{{ vm_baseline_authorized_keys_exclusive }}")

    def test_keys_render_newline_separated(self):
        """The module takes many keys as one newline separated string.

        This renders the template instead of inspecting its source, because
        the source form lies. Jinja does NOT process \\n inside its own string
        literals, so a single quoted YAML scalar produces keys joined by a
        literal backslash-n: the module then writes one mangled line and the
        second key authorises nobody. An earlier version of this test asserted
        the source text matched join("\\n") and passed against exactly that
        bug. Caught by an ansible-playbook --check diff, not by the test.
        """
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar

        keys = yaml.safe_load(DEFAULTS.read_text())["vm_baseline_authorized_keys"]
        template = key_task()["ansible.posix.authorized_key"]["key"]
        rendered = Templar(
            loader=DataLoader(),
            variables={"vm_baseline_authorized_keys": keys},
        ).template(template)

        self.assertEqual(rendered, "\n".join(keys))
        self.assertNotIn("\\n", rendered, "literal backslash-n, not a newline")
        self.assertEqual(len(rendered.splitlines()), len(keys))
        for line, key in zip(rendered.splitlines(), keys):
            self.assertEqual(line, key)


if __name__ == "__main__":
    unittest.main()
