"""Tests for the op attribution shim (files/op-shim).

Run from the role directory:
    python3 -m unittest discover tests

Or from the repo root:
    python3 -m unittest discover roles/op_ratelimit_collector/tests

SAFETY: the shim is always pointed at a fake op via OP_SHIM_REAL_OP. The real
1Password CLI is never executed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROLE = Path(__file__).resolve().parent.parent
SHIM = ROLE / "files" / "op-shim"
DEFAULTS = ROLE / "defaults" / "main.yml"

# Echoes what it received so tests can prove nothing was altered. $$ is the
# pid op runs as, which must equal the pid the shim logged (exec keeps it).
FAKE_OP = r"""#!/bin/bash
echo "$$" >> "$FAKE_CALLS"
printf 'argc=%s\n' "$#" > "$FAKE_ARGV"
for a in "$@"; do printf '[%s]\n' "$a" >> "$FAKE_ARGV"; done
printf '%s' "${FAKE_ENV_PROBE-unset}" > "$FAKE_ENV_OUT"
if [[ -n "${FAKE_STDIN_OUT:-}" ]]; then cat > "$FAKE_STDIN_OUT"; fi
printf 'line1\nline2 \x01\xff no newline at end'
printf 'err-output\n' >&2
exit "${FAKE_RC:-0}"
"""


class ShimSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="op-shim-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = self.tmp / "real-op"
        self.fake.write_text(FAKE_OP)
        self.fake.chmod(0o755)
        self.log = self.tmp / "log" / "op-invocations.log"
        self.log.parent.mkdir()
        self.calls = self.tmp / "calls"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OP_SHIM_REAL_OP": str(self.fake),
            "OP_SHIM_LOG": str(self.log),
            "FAKE_CALLS": str(self.calls),
            "FAKE_ARGV": str(self.tmp / "argv"),
            "FAKE_ENV_OUT": str(self.tmp / "env"),
        }

    def run_shim(self, args: list[str], env: dict | None = None, stdin: bytes = b"",
                 via: list[str] | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        cmd = (via or []) + [str(SHIM), *args]
        return subprocess.run(cmd, env=full_env, input=stdin, capture_output=True,
                              timeout=30, check=False)

    def run_fake_directly(self, args: list[str], env: dict | None = None,
                          ) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run([str(self.fake), *args], env=full_env, capture_output=True,
                              timeout=30, check=False)

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def op_call_count(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0


class LoggingTests(ShimSandbox):
    def test_logs_one_event_with_expected_fields(self) -> None:
        proc = self.run_shim(["read", "op://Infrastructure/Wazuh SIEM/password"],
                             env={"OP_SHIM_SLUG": "wazuh_password"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.events()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(set(ev), {"ts", "pid", "ppid", "unit", "consumer", "caller",
                                   "chain", "parent", "subcommand", "slug", "ref"})
        self.assertIsInstance(ev["ts"], int)
        self.assertLess(abs(ev["ts"] - int(__import__("time").time())), 60)
        self.assertEqual(ev["subcommand"], "read")
        self.assertEqual(ev["slug"], "wazuh_password")
        self.assertEqual(ev["ref"], "op://Infrastructure/Wazuh SIEM/password")
        # exec keeps the pid, so the logged pid is the op process itself.
        self.assertEqual(ev["pid"], int(self.calls.read_text().split()[0]))

    def test_consumer_is_outermost_script_and_caller_is_parent(self) -> None:
        scripts = self.tmp / "scripts"
        scripts.mkdir()
        vault_pass = scripts / "vault-pass.sh"
        vault_pass.write_text(f'#!/bin/bash\n"{SHIM}" read "op://Infrastructure/Ansible Vault Password/password"\n')
        run_proxmox = scripts / "run-proxmox.sh"
        run_proxmox.write_text(f'#!/bin/bash\n"{vault_pass}"\n')
        for s in (vault_pass, run_proxmox):
            s.chmod(0o755)
        proc = subprocess.run([str(run_proxmox)], env=self.env, capture_output=True,
                              timeout=30, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ev = self.events()[0]
        self.assertEqual(ev["consumer"], "run-proxmox")
        self.assertEqual(ev["caller"], "vault-pass")
        self.assertTrue(ev["chain"].startswith("vault-pass.sh<run-proxmox.sh"), ev["chain"])
        self.assertIn("vault-pass.sh", ev["parent"])

    def test_ansible_ancestor_gets_playbook_name(self) -> None:
        fake_ansible = self.tmp / "ansible-playbook"
        fake_ansible.write_text(f'#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(["{SHIM}", "read", "op://v/i/f"])\n')
        fake_ansible.chmod(0o755)
        subprocess.run([str(fake_ansible), "playbooks/proxmox.yml", "--diff"], env=self.env,
                       capture_output=True, timeout=30, check=False)
        self.assertEqual(self.events()[0]["caller"], "ansible-playbook:proxmox.yml")

    def test_subcommand_parsing(self) -> None:
        cases = {
            ("service-account", "ratelimit"): "service-account ratelimit",
            ("item", "get", "Some Item", "--vault", "Infrastructure"): "item get",
            ("--account", "myacct", "vault", "list"): "vault list",
            ("read", "--no-newline", "op://a/b/c"): "read",
            ("--version",): "none",
            ("frobnicate", "x"): "other",
        }
        for args, expected in cases.items():
            self.log.unlink(missing_ok=True)
            with self.subTest(args=args):
                self.run_shim(list(args))
                self.assertEqual(self.events()[0]["subcommand"], expected)

    def test_secret_arguments_never_logged(self) -> None:
        self.run_shim(["item", "create", "--category", "login", "password=hunter2-arg",
                       "--token", "tok-xyz-arg", "op://Infrastructure/Item/field"])
        # The parent is `bash -c <string>`, so the secrets sit in the parent
        # command line the shim logs. The trailing `; true` stops bash from
        # exec'ing the shim in place of itself.
        subprocess.run(
            ["bash", "-c",
             f'OP_SERVICE_ACCOUNT_TOKEN=ops_SECRETTOKEN DB_PASSWORD=hunter2-env "{SHIM}" read op://a/b/c; '
             "true --password hunter2-flag api_key=k3y"],
            env=self.env, capture_output=True, timeout=30, check=False)
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertIn("DB_PASSWORD=REDACTED", events[1]["parent"])
        self.assertIn("--password REDACTED", events[1]["parent"])
        text = self.log.read_text()
        for secret in ["hunter2-arg", "tok-xyz-arg", "ops_SECRETTOKEN", "hunter2-env",
                       "hunter2-flag", "k3y"]:
            self.assertNotIn(secret, text)

    def test_hostile_values_stay_valid_json(self) -> None:
        ref = 'op://Vault/It"em\\with\tcontrol\nchars/' + "x" * 1000
        self.run_shim(["read", ref], env={"OP_SHIM_SLUG": 'bad"slug\n{}'})
        ev = self.events()[0]
        self.assertTrue(ev["ref"].startswith('op://Vault/It"em\\withcontrolchars/'))
        self.assertLessEqual(len(ev["ref"]), 256)
        self.assertEqual(ev["slug"], 'bad"slug{}')

    def test_concurrent_invocations_do_not_interleave(self) -> None:
        procs = [
            subprocess.Popen([str(SHIM), "read", f"op://v/item-{i}/f"], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i in range(25)
        ]
        for p in procs:
            p.wait(timeout=30)
        refs = sorted(ev["ref"] for ev in self.events())
        self.assertEqual(refs, sorted(f"op://v/item-{i}/f" for i in range(25)))

    def test_log_is_not_world_readable(self) -> None:
        self.run_shim(["read", "op://a/b/c"])
        self.assertEqual(self.log.stat().st_mode & 0o007, 0)


class ParentRedactionTests(ShimSandbox):
    """The parent command line is logged, so it must pass an allowlist.

    Every value below is an obviously fake placeholder containing FAKE, so a
    single assertion proves none of them reached the log.
    """

    def setUp(self) -> None:
        super().setUp()
        # Callers find the shim as `op` on PATH, as they would on a host.
        path_dir = self.tmp / "bin"
        path_dir.mkdir()
        (path_dir / "op").symlink_to(SHIM)
        self.env["PATH"] = f"{path_dir}:{self.env['PATH']}"

    def run_via_shell(self, script: str) -> dict:
        # The trailing `; true` stops bash from exec'ing op in place of itself,
        # so the shell stays the parent the shim inspects.
        proc = subprocess.run(["bash", "-c", f"{script}; true"], env=self.env,
                              capture_output=True, timeout=30, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.events()
        self.assertEqual(len(events), 1)
        return events[0]

    def run_via_python(self, op_args: list[str], parent_args: list[str]) -> dict:
        code = f"import subprocess; subprocess.run(['op', *{op_args!r}])"
        proc = subprocess.run([sys.executable, "-c", code, *parent_args], env=self.env,
                              capture_output=True, timeout=30, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.events()
        self.assertEqual(len(events), 1)
        return events[0]

    def assert_no_fake_values(self) -> None:
        self.assertNotIn("FAKE", self.log.read_text())

    def test_field_assignments_redacted_in_shell_parent(self) -> None:
        ev = self.run_via_shell(
            "op item create --vault Infra notesPlain=FAKE-notes credential=FAKE-cred "
            "recoveryPhrase=FAKE-phrase 'My Section.Custom Field[concealed]=FAKE custom value' "
            "apiCred=FAKE\\ escaped\\ spaces")
        for name in ("notesPlain", "credential", "recoveryPhrase",
                     "My Section.Custom Field[concealed]", "apiCred"):
            self.assertIn(f"{name}=REDACTED", ev["parent"])
        self.assertTrue(ev["parent"].startswith("bash -c op item create --vault Infra"),
                        ev["parent"])
        self.assertEqual(ev["subcommand"], "item create")
        self.assert_no_fake_values()

    def test_field_assignments_redacted_in_direct_argv_parent(self) -> None:
        ev = self.run_via_python(
            ["item", "create"],
            ["notesPlain=FAKE notes with spaces", "credential=FAKE-cred",
             "recoveryPhrase=FAKE phrase words", "Custom Field=FAKE-custom", "--vault", "Infra"])
        for name in ("notesPlain", "credential", "recoveryPhrase", "Custom Field"):
            self.assertIn(f"{name}=REDACTED", ev["parent"])
        self.assertIn("--vault Infra", ev["parent"])
        self.assert_no_fake_values()

    def test_non_plain_words_dropped(self) -> None:
        self.run_via_python(
            ["read", "op://a/b/c"],
            ["RkFLRS1iYXNlNjQ=", '{"password":"FAKE-json"}', "--title", "FAKE title words",
             "--token", "FAKEplainword", "OP_SERVICE_ACCOUNT_TOKEN_ALIAS", "ops_FAKE"])
        text = self.log.read_text()
        self.assertNotIn("RkFLRS1iYXNlNjQ", text)
        self.assert_no_fake_values()

    def test_op_read_references_logged_intact(self) -> None:
        ev = self.run_via_shell('op read "op://Infrastructure/Wazuh SIEM/password"')
        self.assertEqual(ev["ref"], "op://Infrastructure/Wazuh SIEM/password")
        self.assertEqual(ev["parent"], "bash -c op read op://Infrastructure/Wazuh SIEM/password; true")
        self.log.unlink()
        ev = self.run_via_shell("op read 'op://Infrastructure/Grafana/one-time password?attribute=otp'")
        self.assertEqual(ev["ref"], "op://Infrastructure/Grafana/one-time password?attribute=otp")
        self.assertIn("op://Infrastructure/Grafana/one-time password?attribute=otp", ev["parent"])

    def test_value_mentioning_a_reference_is_not_logged_as_ref(self) -> None:
        self.run_shim(["item", "edit", "Some Item", "notesPlain=moved to op://FAKE-vault/FAKE-item"])
        ev = self.events()[0]
        self.assertEqual(ev["ref"], "")
        self.assert_no_fake_values()


class TransparencyTests(ShimSandbox):
    ARGS = ["read", "op://Infrastructure/Item With Spaces/pass word", "", "it's \"quoted\"",
            "--flag=value", "*", "$HOME"]

    def test_output_exit_code_and_argv_identical_to_real_op(self) -> None:
        for rc in ("0", "1", "7"):
            with self.subTest(rc=rc):
                direct = self.run_fake_directly(self.ARGS, env={"FAKE_RC": rc, "FAKE_ENV_PROBE": "v"})
                direct_argv = (self.tmp / "argv").read_bytes()
                shimmed = self.run_shim(self.ARGS, env={"FAKE_RC": rc, "FAKE_ENV_PROBE": "v"})
                shim_argv = (self.tmp / "argv").read_bytes()
                self.assertEqual(shimmed.returncode, direct.returncode)
                self.assertEqual(shimmed.stdout, direct.stdout)
                self.assertEqual(shimmed.stderr, direct.stderr)
                self.assertEqual(shim_argv, direct_argv)
                self.assertEqual((self.tmp / "env").read_text(), "v")

    def test_stdin_passed_through(self) -> None:
        payload = b"template {{ op://a/b/c }}\n\x00binary\xff"
        out = self.tmp / "stdin"
        self.run_shim(["inject"], env={"FAKE_STDIN_OUT": str(out)}, stdin=payload)
        self.assertEqual(out.read_bytes(), payload)

    def test_exactly_one_real_op_call(self) -> None:
        self.run_shim(["read", "op://a/b/c"])
        self.assertEqual(self.op_call_count(), 1)

    def test_logging_failure_never_fails_the_read(self) -> None:
        direct = self.run_fake_directly(["read", "op://a/b/c"])
        blocked_dir = self.tmp / "ro"
        blocked_dir.mkdir()
        blocked_dir.chmod(0o500)
        self.addCleanup(blocked_dir.chmod, 0o700)
        is_dir = self.tmp / "is-a-dir"
        is_dir.mkdir()
        hard_capped = self.tmp / "big.log"
        hard_capped.write_bytes(b"x" * 2048)
        broken = {
            "missing parent dir": {"OP_SHIM_LOG": str(self.tmp / "nope" / "x" / "op.log")},
            "unwritable dir": {"OP_SHIM_LOG": str(blocked_dir / "op.log")},
            "log path is a directory": {"OP_SHIM_LOG": str(is_dir)},
            "empty log path": {"OP_SHIM_LOG": ""},
            "hard cap reached": {"OP_SHIM_LOG": str(hard_capped), "OP_SHIM_LOG_HARD_CAP_BYTES": "1024"},
        }
        if os.geteuid() == 0:
            del broken["unwritable dir"]
        for name, env in broken.items():
            with self.subTest(name):
                proc = self.run_shim(["read", "op://a/b/c"], env=env)
                self.assertEqual(proc.returncode, direct.returncode)
                self.assertEqual(proc.stdout, direct.stdout)
                self.assertEqual(proc.stderr, direct.stderr)
        self.assertEqual(hard_capped.stat().st_size, 2048)

    def test_missing_real_op_exits_127(self) -> None:
        proc = self.run_shim(["read", "op://a/b/c"], env={"OP_SHIM_REAL_OP": str(self.tmp / "absent")})
        self.assertEqual(proc.returncode, 127)
        self.assertIn(b"real 1Password CLI not found", proc.stderr)

    def test_refuses_to_exec_itself(self) -> None:
        proc = self.run_shim(["--version"], env={"OP_SHIM_REAL_OP": str(SHIM)})
        self.assertEqual(proc.returncode, 127)


class DefaultsInSyncTests(unittest.TestCase):
    def test_shim_defaults_match_role_defaults(self) -> None:
        shim = SHIM.read_text()
        defaults = DEFAULTS.read_text()
        for shim_var, role_var in (("OP_SHIM_REAL_OP", "op_quota_real_op"),
                                   ("OP_SHIM_LOG", "op_quota_shim_log")):
            shim_default = re.search(rf'^{shim_var}="\$\{{{shim_var}:-([^}}]+)\}}"$', shim, re.M)
            role_default = re.search(rf"^{role_var}: (\S+)$", defaults, re.M)
            self.assertIsNotNone(shim_default, shim_var)
            self.assertIsNotNone(role_default, role_var)
            self.assertEqual(shim_default.group(1), role_default.group(1))


if __name__ == "__main__":
    unittest.main()
