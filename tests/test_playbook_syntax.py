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

SAFETY: this must never reach 1Password. Two separate paths could:

1. The Proxmox dynamic inventory plugin, which is what used to burn read quota.
   The run is pinned to the ini plugin with a temporary fixture inventory.
2. `ansible.cfg` sets `vault_password_file = scripts/vault-pass.sh`, and that
   script calls `cached_op_read`. `--syntax-check` really does invoke it, proven
   by pointing the setting at a script that touches a marker file: the marker
   appears. With a warm cache that costs nothing, with a stale one it is a live
   read, so ANSIBLE_VAULT_PASSWORD_FILE is always overridden here and the repo's
   script is never called.

The repo's callback plugins (which include the 1Password quota gate) are also
pointed at an empty directory.

Five playbooks plus site.yml load vaulted group_vars and genuinely need the real
password to pass a syntax check. On the control node `.vault_pass` exists at the
repo root (gitignored, mode 600) and is used directly, so coverage is complete.
Anywhere else, those playbooks report `Decryption failed` and are reported as
skipped rather than failed, which is the honest answer on a machine that has no
vault password at all.
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
PLAYBOOKS = sorted(
    [p for ext in ("yml", "yaml") for p in (REPO / "playbooks").glob(f"*.{ext}")]
    + [p for p in (REPO / "site.yml", REPO / "site.yaml") if p.is_file()]
)

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
# Gitignored, present on the control node. Never the repo's vault-pass.sh, which
# would call out to 1Password.
VAULT_PASS_FILE = REPO / ".vault_pass"
VAULT_ERROR = "Decryption failed"
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
        cls.vault_available = VAULT_PASS_FILE.is_file()
        if cls.vault_available:
            cls.vault_password_file = VAULT_PASS_FILE
        else:
            # A wrong password, deliberately. It keeps Ansible from calling the
            # repo's 1Password-backed script, and the playbooks that actually
            # need to decrypt something say so plainly.
            cls.vault_password_file = root / "dummy-vault.sh"
            cls.vault_password_file.write_text("#!/bin/sh\necho not-the-real-password\n")
            cls.vault_password_file.chmod(0o700)

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
                # Never scripts/vault-pass.sh. See the SAFETY note above.
                "ANSIBLE_VAULT_PASSWORD_FILE": str(self.vault_password_file),
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
        self.assertTrue(
            any(p.stem == "site" for p in PLAYBOOKS),
            "the root entry point must be checked",
        )
        for playbook in PLAYBOOKS:
            self.assertTrue(playbook.is_file(), f"{playbook} does not exist")

    def test_vault_password_never_comes_from_the_1password_script(self) -> None:
        self.assertNotIn(
            "vault-pass.sh",
            str(self.vault_password_file),
            "the repo's vault script calls cached_op_read; this test must not",
        )

    def test_every_playbook_passes_syntax_check(self) -> None:
        failures = {}
        skipped_for_vault = []
        for playbook in PLAYBOOKS:
            result = self._check(playbook)
            if result.returncode == 0:
                continue
            output = result.stderr + result.stdout
            if VAULT_ERROR in output and not self.vault_available:
                skipped_for_vault.append(playbook.name)
                continue
            failures[playbook.name] = next(
                (
                    line
                    for line in output.splitlines()
                    if line.startswith("ERROR")
                ),
                f"exit {result.returncode}",
            )
        self.assertEqual(failures, {})
        if self.vault_available:
            # On the control node nothing may be skipped: a vaulted playbook
            # that cannot be decrypted with the real password is a real failure.
            self.assertEqual(skipped_for_vault, [])
        elif skipped_for_vault:
            print(
                f"\nno {VAULT_PASS_FILE.name}, so {len(skipped_for_vault)} vaulted "
                f"playbook(s) were not checked: {', '.join(skipped_for_vault)}"
            )
            # Still meaningful: most of the set was checked for real.
            self.assertLess(len(skipped_for_vault), len(PLAYBOOKS) // 2)


if __name__ == "__main__":
    unittest.main()
