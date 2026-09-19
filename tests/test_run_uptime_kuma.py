"""Tests for scripts/run-uptime-kuma.sh and the webhook wiring around it.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 --with ansible-core==2.16.3 \
        pytest tests/test_run_uptime_kuma.py -rs

SAFETY: nothing here can reach 1Password, Discord, Proxmox or a real host. The
wrapper runs with PATH set to a sandbox bin directory holding fakes for op,
ansible-playbook, ansible-inventory, ansible-vault, curl and logger, plus
symlinks to the coreutils it needs. The real /usr/bin/op and
/usr/bin/ansible-playbook are not reachable; a guard test asserts that.

Same harness shape as tests/test_run_ups_shutdown.py: the automation checkout
is a real clone of a throwaway origin holding copies of the real scripts, so
the force-sync, the kill switch and the secret cache all run for real.

What these tests pin down:
  - the webhook comes from the cache, is served stale when 1Password is out,
    and the host is never contacted without one;
  - the webhook reaches only ansible-playbook's environment, never argv, the
    terminal, the log file, or the vault-password helper;
  - the run uses inventory.static.ini alone: no Proxmox API check, no
    ansible-inventory, no Proxmox token. The monitor of last resort must be
    deployable when Proxmox is the thing that is down.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
    "inventory.static.ini",
    "scripts/run-uptime-kuma.sh",
    "scripts/vault-pass.sh",
    "scripts/lib/sync-repo.sh",
    "scripts/lib/op-killswitch.sh",
    "scripts/lib/op-secret-cache.sh",
    "playbooks/uptime-kuma.yml",
]

SLUG = "discord_alerts_webhook_url"
OP_PATH = "op://Infrastructure/vausmfy2q2m57r6scvziyrc7lq/credential"
VAULT_SLUG = "ansible_vault_password"
TTL_SECS = 172800

# Dummy test values shaped like webhooks, not real ones.
CACHED = "https://discord.com/api/webhooks/100000000000000001/cached-token-value-for-tests"
LIVE = "https://discord.com/api/webhooks/100000000000000002/live-token-value-for-tests"

RECORD = r"""
record() {
    python3 - "$0" "$@" <<'PY' >> "$FAKE_LOG"
import json, os, sys
print(json.dumps({
    "tool": os.path.basename(sys.argv[1]),
    "argv": sys.argv[2:],
    "has_pve_token": bool(os.environ.get("PROXMOX_TOKEN_SECRET")),
    "webhook": os.environ.get("UPTIME_KUMA_DISCORD_WEBHOOK_URL"),
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
if [[ "$1" == "read" && "$2" == "op://Infrastructure/vausmfy2q2m57r6scvziyrc7lq/credential" ]]; then
    if [[ -n "${FAKE_WEBHOOK_READ_ERROR:-}" ]]; then
        echo "$FAKE_WEBHOOK_READ_ERROR" >&2
        exit 1
    fi
    printf '%s' "https://discord.com/api/webhooks/100000000000000002/live-token-value-for-tests"
    exit 0
fi
if [[ "$1" == "read" ]]; then
    printf '%s' "vault-password-from-op"
    exit 0
fi
exit 1
"""

FAKE_PLAYBOOK = "#!/bin/bash\n" + RECORD + r"""
record "$@"
scripts/vault-pass.sh > /dev/null || { echo "vault-pass failed"; exit 1; }
echo "PLAY RECAP *********************************************************************"
if [[ "${FAKE_PLAYBOOK_RC:-0}" -ne 0 ]]; then
    echo "uptime-kuma                : ok=3    changed=0    unreachable=0    failed=1    skipped=0"
else
    echo "uptime-kuma                : ok=40   changed=2    unreachable=0    failed=0    skipped=0"
fi
exit "${FAKE_PLAYBOOK_RC:-0}"
"""

FAKE_OTHER = "#!/bin/bash\n" + RECORD + 'record "$@"\nexit 0\n'


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


class WrapperSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="run-uptime-kuma-test-"))
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
        self._script("ansible-playbook", FAKE_PLAYBOOK)
        for name in ("ansible-vault", "ansible-inventory", "curl"):
            self._script(name, FAKE_OTHER)

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
        self.seed(VAULT_SLUG, "cached-vault-password")

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
            [str(self.bin / "bash"), str(self.repo / "scripts" / "run-uptime-kuma.sh"), *args],
            env=full_env, capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, tool: str | None = None) -> list[dict]:
        rows = [json.loads(ln) for ln in self.fake_log.read_text().splitlines() if ln.strip()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def webhook_reads(self) -> list[dict]:
        return [c for c in self.calls("op") if c["argv"] == ["read", OP_PATH]]

    def op_reads(self) -> list[dict]:
        return [c for c in self.calls("op") if c["argv"][:1] == ["read"]]

    def seed(self, slug: str, value: str, age_secs: int = 60) -> Path:
        self.cache.mkdir(mode=0o700, exist_ok=True)
        path = self.cache / slug
        path.write_text(value)
        mtime = time.time() - age_secs
        os.utime(path, (mtime, mtime))
        return path

    def trip_killswitch(self) -> Path:
        lock = self.state / "1p-killswitch"
        lock.write_text("2026-09-19T00:00:00Z trip_reason=test\n")
        return lock

    def playbook_run(self) -> dict:
        runs = self.calls("ansible-playbook")
        self.assertEqual(len(runs), 1, f"expected one ansible-playbook run, got {runs}")
        return runs[0]

    def log_text(self) -> str:
        logs = list(self.logs.glob("uptime-kuma-*.log"))
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


class StaticWiringTests(unittest.TestCase):
    RAW_OP_READ = re.compile(r"""\bop\s+read\s+['"]?op://""")

    def test_playbook_and_role_never_suggest_a_raw_op_read(self) -> None:
        paths = [REPO / "playbooks" / "uptime-kuma.yml",
                 *sorted((REPO / "roles" / "uptime_kuma").rglob("*.yml"))]
        for path in paths:
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertIsNone(self.RAW_OP_READ.search(path.read_text()))

    def test_playbook_points_at_the_wrapper_and_targets_the_static_group(self) -> None:
        text = (REPO / "playbooks" / "uptime-kuma.yml").read_text()
        self.assertIn("scripts/run-uptime-kuma.sh", text)
        self.assertIn("hosts: uptime_kuma", text)

    def test_the_host_is_in_the_static_inventory(self) -> None:
        text = (REPO / "inventory.static.ini").read_text()
        self.assertRegex(text, r"(?m)^\[uptime_kuma\]\nuptime-kuma ansible_host=192\.168\.1\.129 ")
        linux_children = text.split("[linux:children]", 1)[1]
        self.assertIn("uptime_kuma", linux_children.split())
        # host_vars only apply when the directory name matches the host exactly.
        self.assertTrue((REPO / "host_vars" / "uptime-kuma" / "vars.yml").is_file())

    def test_the_labctl_one_off_is_gone(self) -> None:
        self.assertFalse((REPO / "labctl-runs" / "uptime-kuma").exists())


class CachedSecretTests(WrapperSandbox):
    def test_cache_hit_makes_no_op_read_and_uses_only_the_static_inventory(self) -> None:
        self.seed(SLUG, CACHED)
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "900"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = self.playbook_run()
        self.assertEqual(run["webhook"], CACHED)
        self.assertEqual(run["argv"][:3], ["playbooks/uptime-kuma.yml", "-i", "inventory.static.ini"])
        self.assertFalse(run["has_pve_token"])
        self.assertEqual(self.calls("ansible-inventory"), [])
        self.assertEqual(self.calls("curl"), [])
        self.assertEqual(self.calls("ansible-vault"), [])
        self.assertEqual(self.op_reads(), [])

    def test_killswitch_active_serves_stale_cache_without_op(self) -> None:
        self.seed(SLUG, CACHED, age_secs=TTL_SECS + 3600)
        lock = self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(lock.exists())
        self.assertIn("kill switch is active", proc.stderr)
        self.assertEqual(self.playbook_run()["webhook"], CACHED)
        self.assertEqual(self.op_reads(), [])

    def test_op_failure_with_stale_cache_serves_stale_value(self) -> None:
        self.seed(SLUG, CACHED, age_secs=TTL_SECS + 3600)
        proc = self.run_wrapper(env={"FAKE_WEBHOOK_READ_ERROR": "[ERROR] Too many requests"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.webhook_reads()), 1)
        self.assertEqual(self.playbook_run()["webhook"], CACHED)
        self.assertTrue((self.state / "1p-killswitch").exists())

    def test_cold_cache_reads_once_and_caches(self) -> None:
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.webhook_reads()), 1)
        self.assertEqual(self.playbook_run()["webhook"], LIVE)
        cached = self.cache / SLUG
        self.assertEqual(cached.read_text(), LIVE)
        self.assertEqual(cached.stat().st_mode & 0o777, 0o600)

    def test_webhook_reaches_only_the_playbook_env(self) -> None:
        self.seed(SLUG, CACHED)
        proc = self.run_wrapper("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = self.playbook_run()
        self.assertNotIn(CACHED, " ".join(run["argv"]))
        for call in self.calls():
            if call["tool"] != "ansible-playbook":
                self.assertIsNone(call["webhook"], call["tool"])
        self.assertNotIn(CACHED, proc.stdout + proc.stderr)
        self.assertNotIn(CACHED, self.log_text())
        self.assertNotIn("cached-token-value", self.log_text())

    def test_operator_supplied_webhook_needs_no_cache_and_no_op(self) -> None:
        self.trip_killswitch()
        supplied = "https://discord.com/api/webhooks/100000000000000003/operator-supplied-value"
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100",
                                     "UPTIME_KUMA_DISCORD_WEBHOOK_URL": supplied})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.playbook_run()["webhook"], supplied)
        self.assertEqual(self.webhook_reads(), [])
        self.assertFalse((self.cache / SLUG).exists())


class NoWebhookTests(WrapperSandbox):
    def test_op_failure_without_cache_exits_1_and_touches_no_host(self) -> None:
        proc = self.run_wrapper(env={"FAKE_WEBHOOK_READ_ERROR": "[ERROR] Too many requests"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no Discord webhook", proc.stderr)
        self.assertIn("export UPTIME_KUMA_DISCORD_WEBHOOK_URL", proc.stderr)
        self.assertEqual(self.calls("ansible-playbook"), [])
        self.assertIn("no Discord webhook", self.log_text())

    def test_killswitch_without_cache_exits_1_with_no_op_read(self) -> None:
        self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.webhook_reads(), [])
        self.assertEqual(self.calls("ansible-playbook"), [])

    def test_empty_cache_file_is_treated_as_missing(self) -> None:
        self.seed(SLUG, "")
        self.trip_killswitch()
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "100"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.calls("ansible-playbook"), [])


class ArgumentAndFailureTests(WrapperSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.seed(SLUG, CACHED)

    def test_passthrough(self) -> None:
        proc = self.run_wrapper("--check", "--tags", "nfs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.playbook_run()["argv"]
        self.assertEqual(argv[3:], ["--check", "--tags", "nfs"])

    def test_playbook_failure_is_reported_and_propagated(self) -> None:
        proc = self.run_wrapper(env={"FAKE_PLAYBOOK_RC": "2"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("uptime-kuma.yml FAILED rc=2 (failed hosts: uptime-kuma)", proc.stderr)
        self.assertIn("FAILED rc=2", self.log_text())

    def test_repo_sync_failure_refuses_to_run(self) -> None:
        git("remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.repo)
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to run", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_quota_preflight_skips_the_run_when_exhausted(self) -> None:
        proc = self.run_wrapper(env={"FAKE_OP_REMAINING": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OP_QUOTA_GATE_BYPASS=1", proc.stderr)
        self.assertEqual(self.calls("ansible-playbook"), [])


# ---------------------------------------------------------------------------
# The argument guard, checked against the REAL ansible-playbook parser
# ---------------------------------------------------------------------------

# Runs ansible-core's own PlaybookCLI.parse() on an argv and reports what it
# would do. One process per argv: context.CLIARGS can only be set once.
REAL_PARSE = r"""
import io, contextlib, json, sys
from ansible.cli.playbook import PlaybookCLI
from ansible import context
cli = PlaybookCLI(["ansible-playbook", *json.loads(sys.argv[1])])
err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        cli.parse()
except SystemExit:
    print(json.dumps({"error": True}))
    sys.exit(0)
a = context.CLIARGS
print(json.dumps({"inventory": [i.rsplit("/", 1)[-1] for i in (a.get("inventory") or [])],
                  "subset": a.get("subset"), "args": list(a.get("args") or [])}))
"""

SAFE = [
    ["--check"], ["-C"], ["--diff"], ["-D"], ["-vvv"], ["--verbose"], ["--step"],
    ["--syntax-check"], ["--list-tasks"], ["--list-tags"],
    ["--tags", "nfs"], ["-t", "nfs"], ["--tags=nfs"], ["--skip-tags", "docker"],
    ["--start-at-task", "Install the NFS probe"], ["-e", "uptime_kuma_nfs_probe_uid=1000"],
    ["--extra-vars=uptime_kuma_nfs_probe_uid=1000"], ["-t", "nfs", "--check", "-D"],
]


def bypass_candidates() -> list[list[str]]:
    cands: list[list[str]] = []
    for opt in ("--inventory", "--inventory-file", "--limit", "--list-hosts"):
        for n in range(3, len(opt) + 1):
            prefix = opt[:n]
            cands += [[prefix, "x.yml"], [prefix + "=x.yml"]]
    for flags in ("", "v", "vv", "C", "D", "Cv", "vD"):
        cands += [["-" + flags + "i", "x.yml"], ["-" + flags + "ix.yml"],
                  ["-" + flags + "l", "h"], ["-" + flags + "lh"]]
    cands += [["extra.yml"], ["--", "extra.yml"], ["-e", "-lh"], ["--tags", "-ix.yml"],
              ["-e"], ["--tags="], ["-eall"], ["--check", "--lim", "h"], ["-tnfs"]]
    return cands


class RealParserGuardTests(WrapperSandbox):
    """Whatever the wrapper lets through, Ansible itself must see exactly
    inventory.static.ini, no --limit and only playbooks/uptime-kuma.yml."""

    def setUp(self) -> None:
        super().setUp()
        self.seed(SLUG, CACHED)
        try:
            import ansible  # noqa: F401
        except ImportError:
            self.skipTest("ansible-core not importable by this interpreter")
        self.parse_dir = self.tmp / "parse"
        (self.parse_dir / "playbooks").mkdir(parents=True)
        (self.parse_dir / "playbooks" / "uptime-kuma.yml").write_text("[]\n")
        (self.parse_dir / "empty.cfg").write_text("")

    def real_parse(self, argv: list[str]) -> dict:
        proc = subprocess.run(
            [sys.executable, "-c", REAL_PARSE, json.dumps(argv)], cwd=self.parse_dir,
            env={**os.environ, "ANSIBLE_CONFIG": str(self.parse_dir / "empty.cfg")},
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def honoured_bypass(self, extra: list[str]) -> bool:
        """Would ansible-playbook, given these args directly, change the
        inventory, set a limit, or run another playbook?"""
        seen = self.real_parse(["playbooks/uptime-kuma.yml", "-i", "inventory.static.ini", *extra])
        return not seen.get("error") and (
            seen["inventory"] != ["inventory.static.ini"] or seen["subset"] is not None
            or seen["args"] != ["playbooks/uptime-kuma.yml"])

    def test_nothing_the_wrapper_passes_can_change_inventory_limit_or_playbook(self) -> None:
        honoured = 0
        for cand in bypass_candidates() + SAFE:
            with self.subTest(args=cand):
                self.fake_log.write_text("")
                if self.honoured_bypass(cand):
                    honoured += 1
                proc = self.run_wrapper(*cand)
                if proc.returncode != 0:
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(self.calls("ansible-playbook"), [])
                    continue
                seen = self.real_parse(self.playbook_run()["argv"])
                self.assertEqual(seen.get("inventory"), ["inventory.static.ini"], seen)
                self.assertIsNone(seen.get("subset"), seen)
                self.assertEqual(seen.get("args"), ["playbooks/uptime-kuma.yml"], seen)
        # Not vacuous: the candidates include real bypasses of the old guard
        # (--inventory-f, --lim, -vi, -Dlh, ...).
        self.assertGreaterEqual(honoured, 20)

    def test_known_bypasses_are_real_and_refused(self) -> None:
        # A trailing extra playbook is NOT in this list: after the wrapper's
        # `-i inventory.static.ini` argparse rejects it ("unrecognized
        # arguments"). The wrapper refuses it anyway.
        for cand in (["--inventory-f", "x.yml"], ["--lim", "h"], ["--lim=h"], ["-vi", "x.yml"],
                     ["-Dlh"], ["-Ci", "x.yml"], ["-vvl", "h"], ["-ix.yml"]):
            with self.subTest(args=cand):
                self.assertTrue(self.honoured_bypass(cand), f"ansible no longer honours {cand}")
                proc = self.run_wrapper(*cand)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("is not allowed", proc.stderr)

    def test_allowed_forms_pass_through_verbatim(self) -> None:
        for cand in SAFE:
            with self.subTest(args=cand):
                self.fake_log.write_text("")
                proc = self.run_wrapper(*cand)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.playbook_run()["argv"][3:], cand)


if __name__ == "__main__":
    unittest.main()
