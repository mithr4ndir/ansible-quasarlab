"""The Vector role must be able to add its repository key on a minimal image.

On 2026-09-19 vm_baseline failed on a freshly built VM with "gpg: not found",
because the role called gpg without ensuring it was installed. The same task
piped curl into gpg without pipefail, so a failed download would still have
written an empty keyring and reported success.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "roles" / "monitoring" / "vector" / "tasks" / "main.yml"


def load_tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def index_of(tasks: list[dict], needle: str) -> int:
    for i, t in enumerate(tasks):
        if needle.lower() in str(t.get("name", "")).lower():
            return i
    raise AssertionError(f"no task named like {needle!r}")


class VectorRepoKeyTests(unittest.TestCase):
    def test_gnupg_is_installed_before_the_key_task(self):
        tasks = load_tasks()
        key_at = index_of(tasks, "GPG keys for Vector")
        installs_gnupg = [
            i for i, t in enumerate(tasks)
            if "gnupg" in str(t.get("ansible.builtin.apt", {}).get("name", ""))
        ]
        self.assertTrue(installs_gnupg, "no task installs gnupg")
        self.assertLess(
            min(installs_gnupg), key_at,
            "gnupg must be installed BEFORE the task that runs gpg",
        )

    def test_key_task_uses_pipefail_under_bash(self):
        tasks = load_tasks()
        key = tasks[index_of(tasks, "GPG keys for Vector")]
        shell = key.get("ansible.builtin.shell", {})
        cmd = shell.get("cmd", "")
        self.assertIn("pipefail", cmd, "curl | gpg without pipefail can write an empty keyring")
        self.assertEqual(
            shell.get("executable"), "/bin/bash",
            "pipefail is a bash builtin; /bin/sh on Debian is dash and ignores it",
        )

    def test_every_curl_into_gpg_is_guarded(self):
        """Any future curl | gpg in this role needs the same guard."""
        text = TASKS.read_text()
        for line in text.splitlines():
            if "curl" in line and "| gpg" in line:
                block = text[: text.index(line)]
                self.assertIn(
                    "pipefail", block.rsplit("- name:", 1)[-1],
                    f"unguarded pipe into gpg: {line.strip()[:70]}",
                )


if __name__ == "__main__":
    unittest.main()
