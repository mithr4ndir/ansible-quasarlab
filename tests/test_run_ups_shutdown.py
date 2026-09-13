"""Tests for scripts/run-ups-shutdown.sh and the NUT secret wiring around it.

Run from the repo root:
    python3 -m unittest discover tests
    (or: uv run --with pytest pytest tests)

SAFETY: nothing here can reach 1Password, Proxmox or a real host. The wrapper
runs with PATH set to a sandbox bin directory holding fakes for op,
ansible-playbook, ansible-inventory, ansible-vault, curl and logger, plus
symlinks to the coreutils it needs. The real /usr/bin/op and
/usr/bin/ansible-playbook are not reachable; a guard test asserts that.

Same harness shape as tests/test_run_cmd_center.py: the automation checkout is
a real clone of a throwaway origin holding copies of the real scripts, so the
force-sync, the kill switch, the secret cache and resolve-inventory.sh all run
for real.

What these tests pin down is the power-loss safety property. The shutdown
itself is upsmon on each host reading /etc/nut/upsmon.conf and never calls
1Password. This wrapper only (re)deploys that file, and it must be able to do
so from the cache when 1Password is unreachable, rate limited or kill-switched,
and must refuse to touch any host when it has no password at all rather than
overwrite a working upsmon.conf.
"""

from __future__ import annotations

import json
import os
import re
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

COPIED = [
    ".gitignore",
    "scripts/run-ups-shutdown.sh",
    "scripts/resolve-inventory.sh",
    "scripts/vault-pass.sh",
    "scripts/lib/sync-repo.sh",
    "scripts/lib/op-killswitch.sh",
    "scripts/lib/op-secret-cache.sh",
    "scripts/lib/proxmox-vault.sh",
    "playbooks/ups-shutdown.yml",
]

NUT_SLUG = "nut_monitor_password"
NUT_OP_PATH = "op://Infrastructure/NUT upsmon/password"
VAULT_SLUG = "ansible_vault_password"
TTL_SECS = 172800

# Dummy test values, not real secrets.
CACHED_NUT = "cached-nut-value"
LIVE_NUT = "live-nut-value"

# Every fake appends one JSON line per call to $FAKE_LOG. The playbook fake
# also records the NUT password it was handed so the tests can assert which
# source it came from.
RECORD = r"""
record() {
    python3 - "$0" "$@" <<'PY' >> "$FAKE_LOG"
import json, os, sys
print(json.dumps({
    "tool": os.path.basename(sys.argv[1]),
    "argv": sys.argv[2:],
    "has_op_token": bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")),
    "has_pve_token": bool(os.environ.get("PROXMOX_TOKEN_SECRET")),
    "nut_password": os.environ.get("NUT_MONITOR_PASSWORD"),
}))
PY
}
"""

FAKE_OP = "#!/bin/bash\n" + RECORD + r"""
record "$@"
if [[ "$1 $2" == "service-account ratelimit" ]]; then
    [[ "${FAKE_OP_RATELIMIT_FAIL:-0}" == 1 ]] && { echo "network unreachable" >&2; exit 1; }
    [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || { echo "no token" >&2; exit 1; }
    used=$(( 1000 - ${FAKE_OP_REMAINING:-900} ))
    echo "TYPE       ACTION        LIMIT    USED    REMAINING    RESET"
    echo "account    read_write    1000     ${used}     ${FAKE_OP_REMAINING:-900}          N/A"
    exit 0
fi
if [[ "$1" == "read" && "$2" == "op://Infrastructure/NUT upsmon/password" ]]; then
    if [[ -n "${FAKE_NUT_READ_ERROR:-}" ]]; then
        echo "$FAKE_NUT_READ_ERROR" >&2
        exit 1
    fi
    printf '%s' "live-nut-value"
    exit 0
fi
if [[ "$1" == "read" ]]; then
    printf '%s' "vault-password-from-op"
    exit 0
fi
exit 1
"""

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
{"_meta": {"hostvars": {"pve1": {"ansible_host": "192.0.2.11"}}},
 "proxmox": {"hosts": ["pve1"]}}
JSON
"""

FAKE_ANSIBLE_PLAYBOOK = "#!/bin/bash\n" + RECORD + r"""
record "$@"
scripts/vault-pass.sh > /dev/null || { echo "vault-pass failed"; exit 1; }
echo "PLAY RECAP *********************************************************************"
if [[ "${FAKE_PLAYBOOK_RC:-0}" -ne 0 ]]; then
    echo "pve1                       : ok=3    changed=0    unreachable=0    failed=1    skipped=0"
else
    echo "pve1                       : ok=9    changed=1    unreachable=0    failed=0    skipped=0"
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
        self.tmp = Path(tempfile.mkdtemp(prefix="run-ups-shutdown-test-"))
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
            [str(self.bin / "bash"), str(self.repo / "scripts" / "run-ups-shutdown.sh"), *args],
            env=full_env, capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, tool: str | None = None) -> list[dict]:
        rows = [json.loads(ln) for ln in self.fake_log.read_text().splitlines() if ln.strip()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def op_reads(self) -> list[dict]:
        return [c for c in self.calls("op") if c["argv"][:1] == ["read"]]

    def nut_reads(self) -> list[dict]:
        return [c for c in self.op_reads() if c["argv"] == ["read", NUT_OP_PATH]]

    def assert_only_free_op_calls(self) -> None:
        for call in self.calls("op"):
            self.assertEqual(call["argv"], ["service-account", "ratelimit"], call)

    def seed(self, slug: str, value: str, age_secs: int = 60) -> Path:
        self.cache.mkdir(mode=0o700, exist_ok=True)
        path = self.cache / slug
        path.write_text(value)
        mtime = time.time() - age_secs
        os.utime(path, (mtime, mtime))
        return path

    def seed_vault_cache(self) -> None:
        self.seed(VAULT_SLUG, "cached-vault-password")

    def trip_killswitch(self) -> Path:
        lock = self.state / "1p-killswitch"
        lock.write_text("2026-09-13T00:00:00Z trip_reason=test\n")
        return lock

    def playbook_run(self) -> dict:
        runs = self.calls("ansible-playbook")
        self.assertEqual(len(runs), 1, f"expected one ansible-playbook run, got {runs}")
        return runs[0]

    def assert_no_host_touched(self) -> None:
        self.assertEqual(self.calls("ansible-playbook"), [])
        self.assertEqual(self.calls("ansible-inventory"), [])
        self.assertEqual(self.calls("curl"), [])

    def log_text(self) -> str:
        logs = list(self.logs.glob("ups-shutdown-*.log"))
        self.assertEqual(len(logs), 1, logs)
        return logs[0].read_text()


class SandboxGuardTests(WrapperSandbox):
    def test_real_binaries_are_unreachable(self) -> None:
        for tool in ("op", "ansible-playbook", "ansible-vault", "ansible-inventory", "curl"):
            proc = subprocess.run(
                [str(self.bin / "bash"), "-c", f"command -v {tool}"],
                env=self.env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.stdout.strip(), str(self.bin / tool))


class NoRawOpReadTests(unittest.TestCase):
    """The playbook and role used to tell the operator to run a raw `op read`."""

    RAW_OP_READ = re.compile(r"""\bop\s+read\s+['"]?op://""")

    def test_nut_playbook_and_role_never_suggest_a_raw_op_read(self) -> None:
        paths = [REPO / "playbooks" / "ups-shutdown.yml",
                 *sorted((REPO / "roles" / "nut_client").rglob("*.yml"))]
        for path in paths:
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertIsNone(self.RAW_OP_READ.search(path.read_text()))

    def test_playbook_points_at_the_wrapper(self) -> None:
        self.assertIn("scripts/run-ups-shutdown.sh",
                      (REPO / "playbooks" / "ups-shutdown.yml").read_text())
        self.assertIn("scripts/run-ups-shutdown.sh",
                      (REPO / "roles" / "nut_client" / "tasks" / "main.yml").read_text())


class CachedSecretTests(WrapperSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.seed_vault_cache()

    def test_cache_hit_makes_no_op_read(self) -> None:
        self.seed(NUT_SLUG, CACHED_NUT)
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.playbook_run()["nut_password"], CACHED_NUT)
        self.assertEqual(self.op_reads(), [])
        # Only the free quota read ran; nothing billable.
        self.assert_only_free_op_calls()

    def test_killswitch_active_serves_stale_cache_without_op(self) -> None:
        self.seed(NUT_SLUG, CACHED_NUT, age_secs=TTL_SECS + 3600)
        lock = self.trip_killswitch()
        # 100 remaining: above the pre-flight threshold, below the lock's
        # auto-recovery threshold, so the switch really stays tripped.
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(lock.exists())
        self.assertIn("kill switch is active", proc.stderr)
        self.assertEqual(self.playbook_run()["nut_password"], CACHED_NUT)
        self.assertEqual(self.op_reads(), [])
        self.assert_only_free_op_calls()

    def test_killswitch_with_exhausted_quota_runs_from_cache_with_the_bypass(self) -> None:
        # The realistic incident: switch tripped and quota spent. The gate
        # stops the run by default, and the documented bypass lets it apply
        # from the cache with no billable call.
        self.seed(NUT_SLUG, CACHED_NUT, age_secs=TTL_SECS + 3600)
        self.trip_killswitch()
        gated = self.run_wrapper(env={"FAKE_OP_REMAINING": "0"})
        self.assertEqual(gated.returncode, 0, gated.stderr)
        self.assertIn("OP_QUOTA_GATE_BYPASS=1", gated.stderr)
        self.assertEqual(self.calls("ansible-playbook"), [])

        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "0", "OP_QUOTA_GATE_BYPASS": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.playbook_run()["nut_password"], CACHED_NUT)
        self.assertEqual(self.op_reads(), [])

    def test_op_failure_with_stale_cache_serves_stale_value(self) -> None:
        self.seed(NUT_SLUG, CACHED_NUT, age_secs=TTL_SECS + 3600)
        proc = self.run_wrapper(env={"FAKE_NUT_READ_ERROR": "[ERROR] Too many requests"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.nut_reads()), 1)
        self.assertEqual(self.playbook_run()["nut_password"], CACHED_NUT)
        # The rate-limit answer tripped the switch for everything else.
        self.assertTrue((self.state / "1p-killswitch").exists())

    def test_op_unreachable_with_stale_cache_serves_stale_value(self) -> None:
        self.seed(NUT_SLUG, CACHED_NUT, age_secs=TTL_SECS + 3600)
        proc = self.run_wrapper(env={"FAKE_NUT_READ_ERROR": "connection refused",
                                     "FAKE_OP_RATELIMIT_FAIL": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.playbook_run()["nut_password"], CACHED_NUT)

    def test_cold_cache_reads_once_and_caches(self) -> None:
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.nut_reads()), 1)
        self.assertEqual(self.playbook_run()["nut_password"], LIVE_NUT)
        cached = self.cache / NUT_SLUG
        self.assertEqual(cached.read_text(), LIVE_NUT)
        self.assertEqual(cached.stat().st_mode & 0o777, 0o600)

    def test_password_reaches_only_the_playbook_env(self) -> None:
        self.seed(NUT_SLUG, CACHED_NUT)
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "200"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = self.playbook_run()
        self.assertNotIn(CACHED_NUT, " ".join(run["argv"]))
        for tool in ("ansible-inventory", "ansible-vault", "curl"):
            for call in self.calls(tool):
                self.assertIsNone(call["nut_password"], tool)
        self.assertNotIn(CACHED_NUT, proc.stdout + proc.stderr)
        self.assertNotIn(CACHED_NUT, self.log_text())


class NoCachedSecretTests(WrapperSandbox):
    """No cached value and no live read: refuse before any host is contacted.

    Not running is the direction that protects the hardware. Hosts keep the
    upsmon.conf from their last successful run and still shut down on battery.
    Deploying without the password would replace a working config with one
    that cannot log in to the NUT primary, and that host would then ignore a
    power loss.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed_vault_cache()

    def test_op_failure_without_cache_exits_1_and_touches_no_host(self) -> None:
        proc = self.run_wrapper(env={"FAKE_NUT_READ_ERROR": "[ERROR] Too many requests"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no NUT monitor password", proc.stderr)
        self.assertIn("No host was contacted", proc.stderr)
        self.assertIn("export NUT_MONITOR_PASSWORD", proc.stderr)
        self.assertEqual(len(self.nut_reads()), 1)
        self.assert_no_host_touched()
        self.assertEqual(self.calls("ansible-vault"), [])
        self.assertIn("no NUT monitor password", self.log_text())

    def test_killswitch_without_cache_exits_1_with_no_op_read(self) -> None:
        self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no NUT monitor password", proc.stderr)
        self.assertEqual(self.op_reads(), [])
        self.assert_no_host_touched()

    def test_empty_cache_file_is_treated_as_missing(self) -> None:
        self.seed(NUT_SLUG, "")
        self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assert_no_host_touched()

    def test_operator_supplied_password_needs_no_cache_and_no_op(self) -> None:
        self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100",
                                     "NUT_MONITOR_PASSWORD": "operator-supplied"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.playbook_run()["nut_password"], "operator-supplied")
        self.assertEqual(self.op_reads(), [])
        # An operator-supplied value is not written into the cache.
        self.assertFalse((self.cache / NUT_SLUG).exists())


class InventoryAndLogTests(WrapperSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.seed_vault_cache()
        self.seed(NUT_SLUG, CACHED_NUT)

    def test_api_down_uses_resolver_cache_and_logs_to_logfile(self) -> None:
        # resolve-inventory.sh writes its warning to $LOGFILE; under set -u an
        # unset LOGFILE would be an unbound-variable error instead.
        cache = self.repo / "inventory-cache.ini"
        cache.write_text("[proxmox]\npve1 ansible_host=192.0.2.11\n")
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "000"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        argv = self.playbook_run()["argv"]
        self.assertEqual(argv[0], "playbooks/ups-shutdown.yml")
        self.assertEqual(argv[argv.index("-i") + 1], str(cache))
        self.assertIn("using cached inventory", self.log_text())

    def test_api_up_passes_the_vault_token(self) -> None:
        proc = self.run_wrapper(env={"FAKE_CURL_HTTP_CODE": "200"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.calls("ansible-inventory")), 1)
        run = self.playbook_run()
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

    def test_limit_and_passthrough(self) -> None:
        proc = self.run_wrapper("--limit", "pve1", "--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.playbook_run()["argv"]
        self.assertEqual(argv[argv.index("--limit") + 1], "pve1")
        self.assertIn("--check", argv)

    def test_no_limit_by_default(self) -> None:
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--limit", self.playbook_run()["argv"])

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
        self.seed(NUT_SLUG, CACHED_NUT)

    def test_playbook_failure_is_reported_and_propagated(self) -> None:
        proc = self.run_wrapper(env={"FAKE_PLAYBOOK_RC": "2"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("ups-shutdown.yml FAILED rc=2 (failed hosts: pve1)", proc.stderr)
        self.assertIn("FAILED rc=2", self.log_text())

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
