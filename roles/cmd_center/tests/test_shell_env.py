"""Tests for tasks/shell_env.yml and templates/op-ansible-env.sh.j2.

Run from the repo root:
    python3 -m unittest discover roles/cmd_center/tests

SAFETY: nothing here can reach 1Password. Every shell runs with PATH set to a
sandbox bin directory holding a fake `op` that only records its argv, plus a
fake ansible-vault; the real binaries are not reachable (guard test below).

Fixtures are the real startup files from command-center1 on 2026-09-13:
fixtures/bashrc_before is lines 1-19 and 144-145 of ~/.bashrc (the token block,
the non-interactive guard, and the line that sources the profile), and
fixtures/op-ansible-env_before.sh is the unmanaged /etc/profile.d file. Neither
contains a secret value, only the commands that fetch one.

"SSH command" below means `bash -c` with SSH_CLIENT set and a fresh SHLVL,
which is how sshd starts a non-interactive command. Ubuntu builds bash with
SSH_SOURCE_BASHRC, so that shell sources ~/.bashrc. The tests detect a bash
built without it and skip rather than pass vacuously.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROLE = Path(__file__).resolve().parent.parent
REPO = ROLE.parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHELL_ENV = ROLE / "tasks" / "shell_env.yml"
MAIN = ROLE / "tasks" / "main.yml"
TEMPLATE = ROLE / "templates" / "op-ansible-env.sh.j2"

OP_READ_PATH = "op://Infrastructure/Proxmox API/Ansible Inventory/token_secret"

TOOLS = ["bash", "cat", "date", "dirname", "flock", "mkdir", "mktemp", "mv",
         "python3", "rm", "sh", "stat", "timeout", "touch"]

FAKE_OP = r"""#!/bin/bash
echo "$*" >> "$FAKE_OP_CALLS"
printf '%s' "proxmox-token-from-op"
"""

# Runs the vault password script like real ansible-vault, then prints what the
# decrypted vault would contain.
FAKE_ANSIBLE_VAULT = r"""#!/bin/bash
echo "$*" >> "$FAKE_VAULT_CALLS"
[[ "${FAKE_VAULT_FAIL:-0}" == 1 ]] && exit 1
while [[ $# -gt 0 ]]; do
    [[ "$1" == "--vault-password-file" ]] && { "$2" > /dev/null || exit 1; }
    shift
done
echo "vault_proxmox_api_token: proxmox-token-from-vault"
"""


def load_tasks() -> list[dict]:
    return yaml.safe_load(SHELL_ENV.read_text())


def task_by_name(name: str) -> dict:
    return next(t for t in load_tasks() if t["name"] == name)


def apply_replace(task: dict, text: str) -> tuple[str, int]:
    """What ansible.builtin.replace does: re.MULTILINE, then re.subn."""
    args = task["ansible.builtin.replace"]
    return re.subn(re.compile(args["regexp"], re.MULTILINE), args["replace"], text)


REMOVE_READ = "Remove the uncached Proxmox token op read from ~/.bashrc"
FIX_COMMENT = "Correct the stale comment above the service account token export"


class ShellSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shell-env-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not installed")
            (self.bin / tool).symlink_to(found)
        self._script("op", FAKE_OP)
        self._script("ansible-vault", FAKE_ANSIBLE_VAULT)
        self._script("logger", "#!/bin/bash\ncat >/dev/null 2>&1 &\n")
        self.home = self.tmp / "home"
        (self.home / ".config" / "op").mkdir(parents=True)
        (self.home / ".config" / "op" / "service-account-token").write_text("dummy-not-a-token")
        self.op_calls = self.tmp / "op-calls"
        self.op_calls.touch()
        self.vault_calls = self.tmp / "vault-calls"
        self.vault_calls.touch()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "FAKE_OP_CALLS": str(self.op_calls),
            "FAKE_VAULT_CALLS": str(self.vault_calls),
        }

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def shell(self, argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(argv, env=full_env, capture_output=True, text=True,
                              timeout=60, check=False, stdin=subprocess.DEVNULL)

    def ssh_command(self, bashrc: str, count: int = 1) -> None:
        """Run `count` SSH-style non-interactive commands against this ~/.bashrc."""
        (self.home / ".bashrc").write_text(bashrc)
        for _ in range(count):
            proc = self.shell([str(self.bin / "bash"), "-c", "true"],
                              env={"SSH_CLIENT": "192.0.2.10 50000 22"})
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def recorded(self, path: Path) -> list[str]:
        return [ln for ln in path.read_text().splitlines() if ln.strip()]

    def require_ssh_sources_bashrc(self) -> None:
        marker = self.tmp / "bashrc-was-sourced"
        self.ssh_command(f'echo yes > "{marker}"\n')
        if not marker.exists():
            self.skipTest("this bash does not source ~/.bashrc for SSH commands")


class SandboxGuardTests(ShellSandbox):
    def test_real_op_and_ansible_vault_are_unreachable(self) -> None:
        for tool in ("op", "ansible-vault"):
            proc = self.shell([str(self.bin / "bash"), "-c", f"command -v {tool}"])
            self.assertEqual(proc.stdout.strip(), str(self.bin / tool))


class BashrcTests(ShellSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.require_ssh_sources_bashrc()
        self.before = (FIXTURES / "bashrc_before").read_text()

    def fixed(self) -> str:
        text, removed = apply_replace(task_by_name(REMOVE_READ), self.before)
        self.assertEqual(removed, 1)
        text, corrected = apply_replace(task_by_name(FIX_COMMENT), text)
        self.assertEqual(corrected, 1)
        return text

    def test_before_every_ssh_command_calls_op_read(self) -> None:
        # Reproduces the defect: 44 SSH commands would be 44 op reads.
        self.ssh_command(self.before, count=3)
        self.assertEqual(self.recorded(self.op_calls), [f"read {OP_READ_PATH}"] * 3)

    def test_after_ssh_commands_make_no_op_call(self) -> None:
        self.ssh_command(self.fixed(), count=3)
        self.assertEqual(self.recorded(self.op_calls), [])

    def test_after_is_valid_bash_and_keeps_everything_else(self) -> None:
        after = self.fixed()
        (self.tmp / "after").write_text(after)
        proc = self.shell([str(self.bin / "bash"), "-n", str(self.tmp / "after")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("op read", after)
        self.assertNotIn("must be before non-interactive guard", after)
        # The free token-file export, the guard and the profile hook survive.
        self.assertIn('export OP_SERVICE_ACCOUNT_TOKEN="$(cat "$HOME/.config/op/service-account-token")"', after)
        self.assertIn("case $- in", after)
        self.assertIn("source /etc/profile.d/op-ansible-env.sh", after)

    def test_after_still_exports_the_token_file_over_ssh(self) -> None:
        # Other SSH tooling may rely on the free token-file export; it must stay.
        dump = self.tmp / "dump"
        after = self.fixed().replace(
            "# If not running interactively",
            f'echo "${{OP_SERVICE_ACCOUNT_TOKEN:-}}" > "{dump}"\n# If not running interactively', 1)
        self.ssh_command(after)
        self.assertEqual(dump.read_text().strip(), "dummy-not-a-token")

    def test_replace_is_idempotent(self) -> None:
        after = self.fixed()
        for name in (REMOVE_READ, FIX_COMMENT):
            _, count = apply_replace(task_by_name(name), after)
            self.assertEqual(count, 0, name)

    def test_leftover_check_matches_before_and_not_after(self) -> None:
        argv = task_by_name("Look for any op read left in ~/.bashrc")["ansible.builtin.command"]["argv"]
        grep = shutil.which("grep")
        if grep is None:
            self.skipTest("grep not installed")
        for text, expected_rc in ((self.before, 0), (self.fixed(), 1)):
            target = self.tmp / "bashrc-check"
            target.write_text(text)
            proc = subprocess.run([grep, *argv[1:-1], str(target)], capture_output=True, text=True,
                                  check=False)
            self.assertEqual(proc.returncode, expected_rc)


class ProfileTemplateTests(ShellSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "automation-repo"
        for rel in ("scripts/lib/proxmox-vault.sh", "scripts/lib/op-killswitch.sh",
                    "scripts/lib/op-secret-cache.sh", "scripts/vault-pass.sh"):
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        (self.repo / "group_vars" / "all").mkdir(parents=True)
        (self.repo / "group_vars" / "all" / "vault.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n")
        # Warm vault-password cache, as on the host.
        self.cache = self.tmp / "secrets"
        self.cache.mkdir(mode=0o700)
        (self.cache / "ansible_vault_password").write_text("cached-vault-password")
        self.env.update({
            "OP_SECRET_CACHE_DIR": str(self.cache),
            "OP_KILLSWITCH_STATE_DIR": str(self.tmp / "state"),
            "OP_KILLSWITCH_METRIC_FILE": str(self.tmp / "killswitch.prom"),
        })
        template = TEMPLATE.read_text()
        rendered = template.replace("{{ ansible_automation_repo_path }}", str(self.repo))
        self.assertNotRegex(rendered, r"\{\{|\{%|\{#")
        self.profile = self.tmp / "op-ansible-env.sh"
        self.profile.write_text(rendered)

    def source(self, interactive: bool, env: dict | None = None,
               shell: str = "bash") -> subprocess.CompletedProcess:
        script = f'. "{self.profile}"; printf "%s|%s" "${{PROXMOX_TOKEN_SECRET:-unset}}" "${{OP_SERVICE_ACCOUNT_TOKEN:-unset}}"'
        if shell == "bash":
            argv = [str(self.bin / "bash"), "--norc", "--noprofile"]
        else:
            argv = [str(self.bin / "sh")]
        argv += (["-i"] if interactive else []) + ["-c", script]
        return self.shell(argv, env)

    def test_template_is_valid_bash(self) -> None:
        proc = self.shell([str(self.bin / "bash"), "-n", str(self.profile)])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_non_interactive_shell_gets_no_token_and_calls_nothing(self) -> None:
        proc = self.source(interactive=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "unset|dummy-not-a-token")
        self.assertEqual(self.recorded(self.op_calls), [])
        self.assertEqual(self.recorded(self.vault_calls), [])

    def test_interactive_shell_loads_token_from_vault_without_op(self) -> None:
        proc = self.source(interactive=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "proxmox-token-from-vault|dummy-not-a-token")
        self.assertEqual(self.recorded(self.op_calls), [])
        self.assertEqual(len(self.recorded(self.vault_calls)), 1)

    def test_inherited_token_skips_the_decrypt(self) -> None:
        proc = self.source(interactive=True, env={"PROXMOX_TOKEN_SECRET": "inherited"})
        self.assertEqual(proc.stdout, "inherited|dummy-not-a-token")
        self.assertEqual(self.recorded(self.vault_calls), [])
        self.assertEqual(self.recorded(self.op_calls), [])

    def test_vault_failure_warns_and_does_not_fall_back_to_op(self) -> None:
        proc = self.source(interactive=True, env={"FAKE_VAULT_FAIL": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "unset|dummy-not-a-token")
        self.assertIn("could not decrypt PROXMOX_TOKEN_SECRET from vault", proc.stderr)
        self.assertEqual(self.recorded(self.op_calls), [])

    def test_posix_sh_can_source_it(self) -> None:
        # /etc/profile.d is also read by dash login shells.
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                proc = self.source(interactive=interactive, shell="sh")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "unset|dummy-not-a-token")
        self.assertEqual(self.recorded(self.op_calls), [])

    def test_old_unmanaged_profile_called_op_read(self) -> None:
        # Reproduces the defect in the file this template replaces.
        old = FIXTURES / "op-ansible-env_before.sh"
        proc = self.shell([str(self.bin / "bash"), "--norc", "-c", f'source "{old}"'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.recorded(self.op_calls), [f"read {OP_READ_PATH}"])


class TaskStructureTests(unittest.TestCase):
    def test_profile_is_templated_to_the_path_the_startup_files_source(self) -> None:
        task = task_by_name("Manage the Ansible shell environment profile")
        args = task["ansible.builtin.template"]
        self.assertEqual(args["dest"], "/etc/profile.d/op-ansible-env.sh")
        self.assertEqual(args["src"], "op-ansible-env.sh.j2")
        self.assertEqual((args["owner"], args["group"], args["mode"]), ("root", "root", "0644"))
        self.assertEqual(args["validate"], "bash -n %s")
        self.assertIn("/etc/profile.d/op-ansible-env.sh", (FIXTURES / "bashrc_before").read_text())

    def test_bashrc_edits_are_validated_and_run_as_the_owner(self) -> None:
        for name in (REMOVE_READ, FIX_COMMENT):
            task = task_by_name(name)
            self.assertEqual(task["ansible.builtin.replace"]["validate"], "bash -n %s", name)
            self.assertEqual(task["ansible.builtin.replace"]["path"], "/home/{{ ansible_user }}/.bashrc")
            self.assertIs(task["become"], False, name)

    def test_template_never_calls_op(self) -> None:
        code = [ln for ln in TEMPLATE.read_text().splitlines() if not ln.lstrip().startswith("#")]
        self.assertFalse([ln for ln in code
                          if re.search(r"\bop\s+(read|item|run|inject|vault|document)\b", ln)])

    def test_shell_env_is_imported_first(self) -> None:
        imports = [t["ansible.builtin.import_tasks"] for t in yaml.safe_load(MAIN.read_text())]
        self.assertEqual(imports[0], "shell_env.yml")


if __name__ == "__main__":
    unittest.main()
