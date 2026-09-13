"""Tests for scripts/run-cmd-center.sh.

Run from the repo root:
    python3 -m unittest discover tests
    (or: uv run --with pytest pytest tests)

SAFETY: nothing here can reach 1Password, Proxmox or a real host. The wrapper
runs with PATH set to a sandbox bin directory holding fakes for op,
ansible-playbook, ansible-inventory, ansible-vault, curl and logger, plus
symlinks to the coreutils it needs. The real /usr/bin/op and
/usr/bin/ansible-playbook are not reachable; a guard test asserts that.

The automation checkout is a real clone of a throwaway local origin holding a
copy of the real scripts, so the force-sync to origin/main runs for real and
what gets exercised is the code in this repo, not a re-implementation. The fake
ansible tools invoke scripts/vault-pass.sh the way real Ansible does through
vault_password_file, so the secret-cache behaviour across the run is real too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]

TOOLS = [
    "awk", "basename", "bash", "cat", "chmod", "cut", "date", "dirname", "env",
    "find", "flock", "git", "grep", "head", "ls", "mkdir", "mktemp", "mv",
    "paste", "python3", "readlink", "rm", "sed", "sh", "sleep", "sort", "stat",
    "tail", "tee", "timeout", "touch", "tr", "wc", "xargs",
]

# Files copied into the fake automation checkout. The wrapper under test and
# every script it sources are the real ones from this repo.
COPIED = [
    # Real ignore rules: the sync runs `git clean -fd`, which must keep
    # inventory-cache.ini exactly as it does in production.
    ".gitignore",
    "scripts/run-cmd-center.sh",
    "scripts/resolve-inventory.sh",
    "scripts/vault-pass.sh",
    "scripts/lib/sync-repo.sh",
    "scripts/lib/op-killswitch.sh",
    "scripts/lib/op-secret-cache.sh",
    "scripts/lib/proxmox-vault.sh",
    "playbooks/cmd_center.yml",
]

VAULT_SLUG = "ansible_vault_password"

# Every fake appends one JSON line per call to $FAKE_LOG so the tests can
# assert on exactly who ran, with what argv and with which env set.
RECORD = r"""
record() {
    python3 - "$0" "$@" <<'PY' >> "$FAKE_LOG"
import json, os, sys
print(json.dumps({
    "tool": os.path.basename(sys.argv[1]),
    "argv": sys.argv[2:],
    "has_op_token": bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")),
    "has_pve_token": bool(os.environ.get("PROXMOX_TOKEN_SECRET")),
}))
PY
}
"""

FAKE_OP = "#!/bin/bash\n" + RECORD + r"""
record "$@"
if [[ "$1 $2" == "service-account ratelimit" ]]; then
    [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || { echo "no token" >&2; exit 1; }
    used=$(( 1000 - ${FAKE_OP_REMAINING:-900} ))
    echo "TYPE       ACTION        LIMIT    USED    REMAINING    RESET"
    echo "account    read_write    1000     ${used}     ${FAKE_OP_REMAINING:-900}          N/A"
    exit 0
fi
if [[ "$1" == "read" ]]; then
    printf '%s' "vault-password-from-op"
    exit 0
fi
exit 1
"""

# Real Ansible runs vault_password_file once per process; so do these fakes.
FAKE_ANSIBLE_VAULT = "#!/bin/bash\n" + RECORD + r"""
record "$@"
pass_file=""
while [[ $# -gt 0 ]]; do
    [[ "$1" == "--vault-password-file" ]] && pass_file="$2"
    shift
done
"$pass_file" > /dev/null || exit 1
echo "vault_proxmox_api_token: fake-proxmox-token"
"""

FAKE_ANSIBLE_INVENTORY = "#!/bin/bash\n" + RECORD + r"""
record "$@"
scripts/vault-pass.sh > /dev/null || exit 1
cat <<'JSON'
{"_meta": {"hostvars": {"command-center1": {"ansible_host": "192.0.2.88"}}},
 "cmd_center": {"hosts": ["command-center1"]}}
JSON
"""

FAKE_ANSIBLE_PLAYBOOK = "#!/bin/bash\n" + RECORD + r"""
record "$@"
scripts/vault-pass.sh > /dev/null || { echo "vault-pass failed"; exit 1; }
echo "PLAY RECAP *********************************************************************"
if [[ "${FAKE_PLAYBOOK_RC:-0}" -ne 0 ]]; then
    echo "command-center1            : ok=3    changed=0    unreachable=0    failed=1    skipped=0"
else
    echo "command-center1            : ok=9    changed=2    unreachable=0    failed=0    skipped=0"
fi
exit "${FAKE_PLAYBOOK_RC:-0}"
"""

FAKE_CURL = "#!/bin/bash\n" + RECORD + r"""
record "$@"
printf '%s' "${FAKE_CURL_HTTP_CODE:-000}"
"""


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


class WrapperSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="run-cmd-center-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not installed")
            (self.bin / tool).symlink_to(found)
        self._script("logger", "#!/bin/bash\ncat >/dev/null 2>&1 &\n")
        self._script("op", FAKE_OP)
        self._script("ansible-vault", FAKE_ANSIBLE_VAULT)
        self._script("ansible-inventory", FAKE_ANSIBLE_INVENTORY)
        self._script("ansible-playbook", FAKE_ANSIBLE_PLAYBOOK)
        self._script("curl", FAKE_CURL)

        self.repo = self._make_automation_checkout()
        self.home = self.tmp / "home"
        (self.home / ".config" / "op").mkdir(parents=True)
        (self.home / ".config" / "op" / "service-account-token").write_text("dummy-not-a-token")
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.cache = self.tmp / "secrets"
        self.logs = self.tmp / "logs"
        self.fake_log = self.tmp / "fake-calls.jsonl"
        self.fake_log.touch()

        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "ANSIBLE_AUTOMATION_REPO_DIR": str(self.repo),
            "ANSIBLE_LOG_DIR": str(self.logs),
            "ARA_ENV_FILE": str(self.tmp / "no-ara-env.sh"),
            "OP_KILLSWITCH_STATE_DIR": str(self.state),
            "OP_KILLSWITCH_METRIC_FILE": str(self.tmp / "textfiles" / "killswitch.prom"),
            "OP_SECRET_CACHE_DIR": str(self.cache),
            "FAKE_LOG": str(self.fake_log),
        }

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def _make_automation_checkout(self) -> Path:
        work = self.tmp / "work"
        for rel in COPIED:
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        (work / "group_vars" / "all").mkdir(parents=True)
        (work / "group_vars" / "all" / "vault.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n")
        origin = self.tmp / "origin.git"
        git("init", "-q", str(work), cwd=self.tmp)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "fixture", cwd=work)
        git("clone", "-q", "--bare", str(work), str(origin), cwd=self.tmp)
        repo = self.tmp / "automation-repo"
        git("clone", "-q", str(origin), str(repo), cwd=self.tmp)
        return repo

    def run_wrapper(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(
            [str(self.bin / "bash"), str(self.repo / "scripts" / "run-cmd-center.sh"), *args],
            env=full_env, capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, tool: str | None = None) -> list[dict]:
        rows = [json.loads(ln) for ln in self.fake_log.read_text().splitlines() if ln.strip()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def op_reads(self) -> list[dict]:
        return [c for c in self.calls("op") if c["argv"][:1] == ["read"]]

    def seed_vault_cache(self, age_secs: int = 60) -> None:
        self.cache.mkdir(mode=0o700, exist_ok=True)
        path = self.cache / VAULT_SLUG
        path.write_text("cached-vault-password")
        mtime = time.time() - age_secs
        os.utime(path, (mtime, mtime))

    def trip_killswitch(self) -> None:
        (self.state / "1p-killswitch").write_text("2026-09-13T00:00:00Z trip_reason=test\n")

    def playbook_argv(self) -> list[str]:
        runs = self.calls("ansible-playbook")
        self.assertEqual(len(runs), 1, f"expected one ansible-playbook run, got {runs}")
        return runs[0]["argv"]


class SandboxGuardTests(WrapperSandbox):
    def test_real_binaries_are_unreachable(self) -> None:
        for tool in ("op", "ansible-playbook", "ansible-vault", "ansible-inventory", "curl"):
            proc = subprocess.run(
                [str(self.bin / "bash"), "-c", f"command -v {tool}"],
                env=self.env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.stdout.strip(), str(self.bin / tool))


class KillSwitchTests(WrapperSandbox):
    def test_active_killswitch_makes_no_billable_op_call_and_runs_nothing(self) -> None:
        self.trip_killswitch()
        # Quota still low, so the lock's auto-recovery does not release it.
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "10"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("kill switch is active", proc.stderr)
        self.assertEqual(self.op_reads(), [])
        # The only op call allowed is the free quota read inside the lock check.
        for call in self.calls("op"):
            self.assertEqual(call["argv"], ["service-account", "ratelimit"])
        self.assertEqual(self.calls("ansible-vault"), [])
        self.assertEqual(self.calls("ansible-inventory"), [])
        self.assertEqual(self.calls("ansible-playbook"), [])

    def test_quota_preflight_reads_quota_with_a_token(self) -> None:
        # Regression for the ordering bug in run-proxmox.sh/run-security.sh:
        # the pre-flight ran before the token was exported, could never read
        # the quota, and failed open on every fire.
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "5"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("quota is down to 5", proc.stderr)
        ratelimit = [c for c in self.calls("op") if c["argv"] == ["service-account", "ratelimit"]]
        self.assertTrue(ratelimit)
        self.assertTrue(all(c["has_op_token"] for c in ratelimit))
        self.assertEqual(self.calls("ansible-playbook"), [])
        self.assertEqual(self.op_reads(), [])


class SecretCacheTests(WrapperSandbox):
    def test_cold_cache_reads_vault_password_once_for_the_whole_run(self) -> None:
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "200"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Non-vacuous: vault-pass.sh really ran in several ansible processes.
        self.assertEqual(len(self.calls("ansible-vault")), 1)
        self.assertEqual(len(self.calls("ansible-inventory")), 1)
        self.assertEqual(len(self.calls("ansible-playbook")), 1)
        reads = self.op_reads()
        self.assertEqual(len(reads), 1, reads)
        self.assertEqual(reads[0]["argv"], ["read", "op://Infrastructure/Ansible Vault Password/password"])
        self.assertTrue((self.cache / VAULT_SLUG).is_file())

    def test_warm_cache_makes_no_op_read(self) -> None:
        self.seed_vault_cache()
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "200"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.calls("ansible-playbook")), 1)
        self.assertEqual(self.op_reads(), [])


class InventoryTests(WrapperSandbox):
    def test_api_down_uses_the_resolver_cache_not_the_dynamic_inventory(self) -> None:
        self.seed_vault_cache()
        cache = self.repo / "inventory-cache.ini"
        cache.write_text("[cmd_center]\ncommand-center1 ansible_host=192.0.2.88\n")
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "000"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.playbook_argv()
        self.assertEqual(argv[argv.index("-i") + 1], str(cache))
        self.assertEqual(self.calls("ansible-inventory"), [])
        # The resolver probed the API with the vault-decrypted token.
        self.assertEqual(len(self.calls("curl")), 1)
        self.assertTrue(self.calls("curl")[0]["has_pve_token"])

    def test_api_up_refreshes_the_resolver_cache_and_passes_the_vault_token(self) -> None:
        self.seed_vault_cache()
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "200"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.calls("ansible-inventory")), 1)
        self.assertIn("command-center1", (self.repo / "inventory-cache.ini").read_text())
        run = self.calls("ansible-playbook")[0]
        self.assertNotIn("-i", run["argv"])
        self.assertTrue(run["has_pve_token"])

    def test_caller_cannot_bypass_the_resolver(self) -> None:
        for bad in (["-i", "inventory.proxmox.yml"], ["--inventory=inventory.proxmox.yml"],
                    ["-iinventory.proxmox.yml"]):
            with self.subTest(args=bad):
                proc = self.run_wrapper(*bad)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("not allowed", proc.stderr)
        self.assertEqual(self.calls(), [])


class LimitAndArgsTests(WrapperSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.seed_vault_cache()

    def test_defaults_to_command_center1(self) -> None:
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.playbook_argv()
        self.assertEqual(argv[0], "playbooks/cmd_center.yml")
        self.assertEqual(argv[argv.index("--limit") + 1], "command-center1")
        self.assertEqual(argv.count("--limit"), 1)

    def test_caller_overrides_limit_and_passes_extra_args(self) -> None:
        proc = self.run_wrapper("--limit", "k8cluster1", "--check", "--tags", "shell_env")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.playbook_argv()
        self.assertEqual(argv[argv.index("--limit") + 1], "k8cluster1")
        self.assertEqual(argv.count("--limit"), 1)
        self.assertNotIn("command-center1", argv)
        self.assertIn("--check", argv)
        self.assertEqual(argv[argv.index("--tags") + 1], "shell_env")

    def test_rejects_unsafe_limit_patterns(self) -> None:
        for bad in ("@hosts.txt", "-e", "a;touch /tmp/x", "a b", "$(id)", ""):
            with self.subTest(limit=bad):
                proc = self.run_wrapper("--limit", bad)
                self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.calls(), [])


class FailureReportingTests(WrapperSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.seed_vault_cache()

    def test_playbook_failure_is_reported_and_propagated(self) -> None:
        proc = self.run_wrapper(env={"FAKE_PLAYBOOK_RC": "2"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cmd_center.yml FAILED rc=2 (failed hosts: command-center1)", proc.stderr)
        logs = list(self.logs.glob("cmd-center-*.log"))
        self.assertEqual(len(logs), 1)
        log = logs[0].read_text()
        self.assertIn("FAILED rc=2", log)
        self.assertIn("failed=1", log)

    def test_success_exits_zero_and_logs(self) -> None:
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cmd_center.yml succeeded (changed hosts: command-center1)", proc.stdout)

    def test_repo_sync_failure_refuses_to_run(self) -> None:
        git("remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.repo)
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to run", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_vault_decrypt_failure_refuses_to_run(self) -> None:
        self._script("ansible-vault", "#!/bin/bash\nexit 1\n")
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("failed to decrypt", proc.stderr)
        self.assertEqual(self.calls("ansible-playbook"), [])


if __name__ == "__main__":
    unittest.main()
