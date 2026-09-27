"""Every playbook must pass `ansible-playbook --syntax-check`.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest tests/test_playbook_syntax.py

Why this exists: `playbooks/k8s_init.yml` could not run at all. A bulk FQCN
rewrite to satisfy lint (cc86f39) had left `community.general.dpkg_selections`
in roles/k8s/kube_pkg, and that module does not exist in the installed
community.general, so the play died with "couldn't resolve module/action".
Lint had passed, the YAML was valid, and nothing else would have caught it
until the next control-plane bootstrap.

A syntax check is not deployment validation. It does resolve every module name
against the collections actually installed, which is the class of breakage that
a lint-clean, YAML-valid playbook can still carry.

SAFETY: this must never resolve the Proxmox dynamic inventory. That plugin is
what used to burn 1Password read quota, so the run is pinned to the ini plugin
with a temporary fixture inventory, and the repo's callback plugins (which
include the 1Password quota gate) are pointed at an empty directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
# site.yml at the repo root is a playbook as well: it imports the others, so a
# malformed import there breaks everything while playbooks/ stays clean.
PLAYBOOKS = sorted((REPO / "playbooks").glob("*.yml")) + [REPO / "site.yml"]

# A syntax check resolves module names against the collections it can see, so it
# is only meaningful against the set the control node actually has. The pinned
# runner installs ansible-core alone, which sees no collections at all and would
# fail every playbook for the wrong reason, so the search path is pointed at the
# system collections. The precheck below skips rather than passes when nothing
# resolves, because a vacuous green here would be worse than no test.
COLLECTION_PATHS = ":".join(
    str(p)
    for p in (
        Path.home() / ".ansible/collections",
        Path("/usr/lib/python3/dist-packages"),
    )
    if (p / "ansible_collections").is_dir()
)
CANARY_MODULE = "kubernetes.core.k8s"
FIXTURE_INVENTORY = """\
[cmd_center]
fixture-cc ansible_host=127.0.0.1 ansible_connection=local

[k8s_control_plane]
fixture-k8s ansible_host=127.0.0.1 ansible_connection=local

[k8s:children]
k8s_control_plane

[linux:children]
cmd_center
k8s
"""


def _collections_are_visible() -> bool:
    """Can a module from a declared collection be resolved at all?"""
    if shutil.which("ansible-doc") is None:
        return False
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTION_PATHS)
    result = subprocess.run(
        ["ansible-doc", "-t", "module", CANARY_MODULE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


@unittest.skipIf(
    shutil.which("ansible-playbook") is None, "ansible-playbook is not installed"
)
class PlaybookSyntaxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not _collections_are_visible():
            raise unittest.SkipTest(
                f"{CANARY_MODULE} does not resolve with "
                f"ANSIBLE_COLLECTIONS_PATH={COLLECTION_PATHS}; a syntax check "
                "here would fail for the wrong reason"
            )
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.inventory = root / "fixture.ini"
        cls.inventory.write_text(FIXTURE_INVENTORY)
        cls.no_callbacks = root / "no-callbacks"
        cls.no_callbacks.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _check(self, playbook: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            {
                # Never the Proxmox plugin. See the SAFETY note above.
                "ANSIBLE_INVENTORY_ENABLED": "ini",
                "ANSIBLE_INVENTORY": str(self.inventory),
                # ANSIBLE_CALLBACK_PLUGINS REPLACES ansible.cfg's setting.
                "ANSIBLE_CALLBACK_PLUGINS": str(self.no_callbacks),
                "ANSIBLE_CALLBACKS_ENABLED": "",
                # Resolve modules against the control node's real collections.
                "ANSIBLE_COLLECTIONS_PATH": COLLECTION_PATHS,
            }
        )
        return subprocess.run(
            ["ansible-playbook", "--syntax-check", str(playbook)],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_there_are_playbooks_to_check(self) -> None:
        # Guards against the glob finding nothing and the suite passing empty.
        self.assertGreaterEqual(len(PLAYBOOKS), 15, f"found {PLAYBOOKS}")
        self.assertIn(
            REPO / "site.yml", PLAYBOOKS, "the root entry point must be checked"
        )
        for playbook in PLAYBOOKS:
            self.assertTrue(playbook.is_file(), f"{playbook} does not exist")

    def test_every_playbook_passes_syntax_check(self) -> None:
        failures = {}
        for playbook in PLAYBOOKS:
            result = self._check(playbook)
            if result.returncode != 0:
                first_error = next(
                    (
                        line
                        for line in (result.stderr + result.stdout).splitlines()
                        if line.startswith("ERROR")
                    ),
                    f"exit {result.returncode}",
                )
                failures[playbook.name] = first_error
        self.assertEqual(failures, {})


if __name__ == "__main__":
    unittest.main()
