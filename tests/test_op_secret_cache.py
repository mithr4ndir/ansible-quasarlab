"""Tests for scripts/lib/op-secret-cache.sh and scripts/op-secret-refresh.sh.

Run from the repo root:
    python3 -m unittest discover tests
    (or: pytest tests)

SAFETY: these tests must never reach 1Password. Every bash process runs with
PATH set to a sandbox bin directory that holds a fake `op` plus symlinks to
the handful of coreutils the library needs, so the real /usr/bin/op is not
reachable even by accident. A guard test asserts that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib" / "op-secret-cache.sh"
REFRESH = REPO / "scripts" / "op-secret-refresh.sh"

TOOLS = [
    "bash", "cat", "chmod", "date", "dirname", "flock", "ls", "mkdir",
    "mktemp", "mv", "readlink", "rm", "sed", "sleep", "sort", "stat",
    "touch", "tr",
]

# Records each call, then behaves as configured through OP_FAKE_* env vars.
FAKE_OP = r"""#!/bin/bash
echo "$$ $*" >> "$OP_FAKE_CALLS"
if [[ -n "${OP_FAKE_FD_DUMP:-}" ]]; then
    ls -l /proc/$$/fd/ > "$OP_FAKE_FD_DUMP" 2>&1
fi
sleep "${OP_FAKE_SLEEP:-0}"
if [[ -n "${OP_FAKE_STDERR:-}" ]]; then
    printf '%s\n' "$OP_FAKE_STDERR" >&2
fi
printf '%s' "${OP_FAKE_VALUE-}"
exit "${OP_FAKE_RC:-0}"
"""


class CacheSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="op-cache-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not installed")
            (self.bin / tool).symlink_to(found)
        # logger stub: keep test noise out of the host syslog.
        self._script("logger", "#!/bin/bash\ncat >/dev/null 2>&1 &\necho \"$*\" >> \"$OP_FAKE_LOGGER\"\n")
        self._script("op", FAKE_OP)
        self.cache = self.tmp / "secrets"
        self.calls = self.tmp / "op-calls"
        self.calls.touch()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp),
            "OP_SECRET_CACHE_DIR": str(self.cache),
            "OP_SERVICE_ACCOUNT_TOKEN": "dummy-not-a-token",
            "OP_FAKE_CALLS": str(self.calls),
            "OP_FAKE_LOGGER": str(self.tmp / "logger"),
            "OP_FAKE_VALUE": "from-op",
        }

    def _script(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def bash(self, script: str, env: dict | None = None, timeout: float = 30,
             ) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(
            [str(self.bin / "bash"), "-c", f'source "{LIB}"\n{script}'],
            env=full_env, capture_output=True, text=True, timeout=timeout, check=False,
        )

    def read(self, slug: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return self.bash(f'cached_op_read {slug} "op://Infrastructure/Item {slug}/password"', env)

    def op_calls(self) -> list[str]:
        return [ln for ln in self.calls.read_text().splitlines() if ln.strip()]

    def seed(self, slug: str, value: str, age_secs: int = 0) -> Path:
        self.cache.mkdir(mode=0o700, exist_ok=True)
        path = self.cache / slug
        path.write_text(value)
        mtime = time.time() - age_secs
        os.utime(path, (mtime, mtime))
        return path


class SandboxGuardTests(CacheSandbox):
    def test_real_op_is_unreachable(self) -> None:
        proc = self.bash("command -v op")
        self.assertEqual(proc.stdout.strip(), str(self.bin / "op"))


class ReadTests(CacheSandbox):
    def test_fresh_cache_served_without_op(self) -> None:
        self.seed("wazuh_password", "cached-value", age_secs=60)
        proc = self.read("wazuh_password")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cached-value")
        self.assertEqual(self.op_calls(), [])

    def test_miss_calls_op_once_and_writes_cache(self) -> None:
        value = "p@ss word with 'quotes' and \"dq\" $HOME"
        proc = self.read("grafana_pg_password", {"OP_FAKE_VALUE": value})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, value)
        calls = self.op_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("read op://Infrastructure/Item grafana_pg_password/password", calls[0])
        cache_file = self.cache / "grafana_pg_password"
        self.assertEqual(cache_file.read_text(), value)
        self.assertEqual(cache_file.stat().st_mode & 0o777, 0o600)
        # No temp files left behind; only the value and its lock file.
        self.assertEqual(
            sorted(p.name for p in self.cache.iterdir()),
            [".grafana_pg_password.lock", "grafana_pg_password"],
        )

    def test_works_under_errexit_like_vault_pass(self) -> None:
        # vault-pass.sh runs with set -euo pipefail. Neither the miss path
        # nor the lock handling may abort it.
        proc = self.bash(
            "set -euo pipefail\n"
            'if value=$(cached_op_read ansible_vault_password "op://Infrastructure/Ansible Vault Password/password"); then\n'
            '    printf "%s" "$value"\nfi\n'
            'value=$(cached_op_read ansible_vault_password "op://x/y/z")\n'
            'printf "|%s" "$value"\n'
            'op_secret_cache_invalidate ansible_vault_password\n'
            'printf "|done"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "from-op|from-op|done")
        self.assertEqual(len(self.op_calls()), 1)

    def test_op_failure_serves_stale(self) -> None:
        self.seed("authentik_secret_key", "old", age_secs=10 * 86400)
        proc = self.read("authentik_secret_key", {"OP_FAKE_VALUE": "", "OP_FAKE_RC": "1"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "old")
        self.assertEqual(len(self.op_calls()), 1)

    def test_op_failure_without_cache_returns_1(self) -> None:
        proc = self.read("nothing_here", {"OP_FAKE_VALUE": "", "OP_FAKE_RC": "1"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")

    def test_invalid_slug_rejected_without_op(self) -> None:
        for slug in ["../escape", ".hidden", "a/b", "'x y'"]:
            with self.subTest(slug=slug):
                proc = self.read(slug)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
        self.assertEqual(self.op_calls(), [])
        self.assertFalse((self.tmp / "escape").exists())

    def test_op_call_is_tagged_with_slug_for_the_shim(self) -> None:
        self._script("op", FAKE_OP.replace(
            'echo "$$ $*"', 'echo "slug=${OP_SHIM_SLUG:-} $*"'))
        self.read("wazuh_api_password")
        self.assertTrue(self.op_calls()[0].startswith("slug=wazuh_api_password read "))


class TtlTests(CacheSandbox):
    def test_default_ttl_is_48h(self) -> None:
        proc = self.bash('printf %s "$OP_SECRET_CACHE_TTL_SECS"')
        self.assertEqual(proc.stdout, "172800")

    def test_entry_younger_than_ttl_is_fresh(self) -> None:
        # 47h old: stale under the old 12h default, fresh under 48h.
        self.seed("authentik_admin_email", "cached", age_secs=47 * 3600)
        proc = self.read("authentik_admin_email")
        self.assertEqual(proc.stdout, "cached")
        self.assertEqual(self.op_calls(), [])

    def test_entry_older_than_ttl_is_refreshed(self) -> None:
        self.seed("authentik_admin_email", "cached", age_secs=49 * 3600)
        proc = self.read("authentik_admin_email")
        self.assertEqual(proc.stdout, "from-op")
        self.assertEqual(len(self.op_calls()), 1)
        self.assertEqual((self.cache / "authentik_admin_email").read_text(), "from-op")

    def test_ttl_override_honored(self) -> None:
        self.seed("short", "cached", age_secs=120)
        proc = self.read("short", {"OP_SECRET_CACHE_TTL_SECS": "60"})
        self.assertEqual(proc.stdout, "from-op")
        self.assertEqual(len(self.op_calls()), 1)


class LockingTests(CacheSandbox):
    def test_concurrent_misses_collapse_to_one_op_call(self) -> None:
        env = dict(self.env, OP_FAKE_SLEEP="1.5", OP_FAKE_VALUE="shared-value")
        script = f'source "{LIB}"\ncached_op_read ansible_vault_password "op://Infrastructure/Ansible Vault Password/password"'
        procs = [
            subprocess.Popen(
                [str(self.bin / "bash"), "-c", script], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(8)
        ]
        results = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
        for out, err, rc in results:
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, "shared-value")
        self.assertEqual(len(self.op_calls()), 1, self.op_calls())

    def test_concurrent_misses_on_different_slugs_do_not_block_each_other(self) -> None:
        env = dict(self.env, OP_FAKE_SLEEP="2")
        start = time.monotonic()
        procs = [
            subprocess.Popen(
                [str(self.bin / "bash"), "-c", f'source "{LIB}"\ncached_op_read slug_{i} op://v/i/f'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(4)
        ]
        for p in procs:
            p.communicate(timeout=60)
        self.assertLess(time.monotonic() - start, 6)
        self.assertEqual(len(self.op_calls()), 4)

    def test_one_process_reading_several_slugs_does_not_deadlock(self) -> None:
        proc = self.bash(
            "load_cached_secrets <<'S'\n"
            "A_ONE    slug_one    op://Infrastructure/One/password\n"
            "A_TWO    slug_two    op://Infrastructure/Two/password\n"
            "A_THREE  slug_three  op://Infrastructure/Three/password\n"
            "S\n"
            'value=$(cached_op_read slug_one op://Infrastructure/One/password)\n'
            'printf "%s|%s|%s|%s" "$A_ONE" "$A_TWO" "$A_THREE" "$value"',
            {"OP_SECRET_CACHE_LOCK_TIMEOUT_SECS": "3"},
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "from-op|from-op|from-op|from-op")
        self.assertEqual(len(self.op_calls()), 3)

    def test_lock_fd_is_not_inherited_by_op(self) -> None:
        dump = self.tmp / "fds"
        self.read("fd_check", {"OP_FAKE_FD_DUMP": str(dump)})
        self.assertTrue(dump.exists())
        self.assertNotIn(".lock", dump.read_text())

    def test_lock_timeout_serves_stale_instead_of_calling_op(self) -> None:
        self.seed("held", "stale-value", age_secs=10 * 86400)
        holder = subprocess.Popen(
            [str(self.bin / "flock"), str(self.cache / ".held.lock"), str(self.bin / "sleep"), "5"],
            env=self.env,
        )
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        proc = self.read("held", {"OP_SECRET_CACHE_LOCK_TIMEOUT_SECS": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "stale-value")
        self.assertEqual(self.op_calls(), [])


class ForcedRefreshTests(CacheSandbox):
    def refresh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.bin / "bash"), str(REFRESH), *args],
            env=self.env, capture_output=True, text=True, timeout=30, check=False,
        )

    def test_refresh_one_slug(self) -> None:
        self.seed("grafana_pg_password", "old", age_secs=60)
        self.seed("wazuh_password", "untouched", age_secs=60)
        proc = self.refresh("grafana_pg_password")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.op_calls(), [], "refresh itself must not call op")
        self.assertEqual(self.read("grafana_pg_password", {"OP_FAKE_VALUE": "new"}).stdout, "new")
        self.assertEqual(self.read("wazuh_password").stdout, "untouched")
        # Refreshed exactly once: the second read is served from cache.
        self.assertEqual(self.read("grafana_pg_password", {"OP_FAKE_VALUE": "newer"}).stdout, "new")
        self.assertEqual(len(self.op_calls()), 1)

    def test_refresh_all(self) -> None:
        for slug in ("a_one", "b_two", "c_three"):
            self.seed(slug, "old", age_secs=60)
        proc = self.refresh("--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("3 slug(s)", proc.stdout)
        for slug in ("a_one", "b_two", "c_three"):
            self.assertEqual(self.read(slug, {"OP_FAKE_VALUE": "new"}).stdout, "new")
        self.assertEqual(len(self.op_calls()), 3)

    def test_refreshed_slug_keeps_old_value_if_op_fails(self) -> None:
        self.seed("vault_pw", "old", age_secs=60)
        self.refresh("vault_pw")
        proc = self.read("vault_pw", {"OP_FAKE_VALUE": "", "OP_FAKE_RC": "1"})
        self.assertEqual(proc.stdout, "old")

    def test_refresh_rejects_bad_slug(self) -> None:
        self.seed("real", "old", age_secs=60)
        proc = self.refresh("../real")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.read("real").stdout, "old")

    def test_list_shows_no_values(self) -> None:
        self.seed("secret_slug", "SUPERSECRETVALUE", age_secs=60)
        proc = self.refresh("--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("secret_slug", proc.stdout)
        self.assertIn("fresh", proc.stdout)
        self.assertNotIn("SUPERSECRETVALUE", proc.stdout)

    def test_no_args_is_usage_error(self) -> None:
        self.assertEqual(self.refresh().returncode, 2)


if __name__ == "__main__":
    unittest.main()
