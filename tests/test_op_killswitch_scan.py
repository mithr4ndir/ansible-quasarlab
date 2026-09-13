"""Tests for the 1Password kill switch rate-limit scanners (issue #160).

Run from the repo root:
    python3 -m unittest discover tests
    (or: uv run --with pytest pytest tests)

On 2026-09-13 the kill switch tripped with trip_reason=rate_limited while
1Password was refusing nothing. The wrappers grepped the whole ansible-playbook
output for "Too many requests", and the --diff of the newly deployed
op-killswitch.sh contained that phrase in its own comments.
fixtures/killswitch_selfdiff_20260913.log is that diff, cut from
/var/log/ansible-quasarlab/cmd-center-20260913-204431.log.

Two scanners, two kinds of input:
- op_killswitch_scan_file: output captured from an op process and nothing else
  (cached_op_read, the quota collector). Deliberately broad.
- op_killswitch_scan_playbook_output: a whole ansible-playbook log. Trips only
  on the op CLI error line format, never on the phrase in a diff line.

The real op error text used below comes from:
- https://www.1password.dev/service-accounts/rate-limits (hourly and daily
  limit errors, "(429) Too Many Requests: You've reached the maximum number
  of this type of requests ...").
- The op 2.39.0 binary on command-center1, which embeds the string
  "Too many requests. Your client has been rate-limited." and writes errors to
  stderr as "[ERROR] YYYY/MM/DD HH:MM:SS <message>" (observed from a free,
  credential-less `op service-account ratelimit`).

SAFETY: nothing here can reach 1Password. Every bash process runs with PATH
set to a sandbox bin directory holding a fake op and symlinks to coreutils.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib" / "op-killswitch.sh"
ROLE_LIB = REPO / "roles" / "op_ratelimit_collector" / "files" / "op-killswitch.sh"
CACHE_LIB = REPO / "scripts" / "lib" / "op-secret-cache.sh"
FIXTURE = REPO / "tests" / "fixtures" / "killswitch_selfdiff_20260913.log"

TOOLS = [
    "bash", "cat", "chmod", "date", "dirname", "flock", "grep", "mkdir",
    "mktemp", "mv", "rm", "stat", "touch",
]

# Documented hourly and daily limit errors, in the stderr layout op uses.
OP_HOURLY = (
    "[ERROR] 2026/09/13 20:46:05 (429) Too Many Requests: You've reached the "
    "maximum number of this type of requests this service account is allowed "
    "to make. Please retry in 59 minutes or try other requests."
)
OP_DAILY = (
    "[ERROR] 2026/09/13 20:46:05 (429) Too Many Requests: You've reached the "
    "maximum number of this type of requests this 1Password account is allowed "
    "to make. Please retry in 23 hours and 59 minutes or try other requests."
)
# Embedded in the op 2.39.0 binary.
OP_CLIENT_THROTTLED = (
    "[ERROR] 2026/09/13 20:46:05 Too many requests. Your client has been rate-limited."
)

FAKE_OP = r"""#!/bin/bash
echo "$*" >> "$OP_FAKE_CALLS"
[[ -n "${OP_FAKE_STDERR:-}" ]] && printf '%s\n' "$OP_FAKE_STDERR" >&2
exit "${OP_FAKE_RC:-1}"
"""


def ansible_fatal(stderr: str) -> str:
    """A failed command task as the default callback prints it: one line."""
    escaped = stderr.replace('"', '\\"')
    return (
        "TASK [example : List vault items] *********************************\n"
        'fatal: [command-center1]: FAILED! => {"changed": true, "cmd": ["op", '
        '"item", "list"], "delta": "0:00:01.2", "msg": "non-zero return code", '
        f'"rc": 1, "stderr": "{escaped}", "stderr_lines": ["{escaped}"], '
        '"stdout": "", "stdout_lines": []}\n'
    )


class KillSwitchSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="op-killswitch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not installed")
            (self.bin / tool).symlink_to(found)
        # logger stub: keep test noise out of the host syslog.
        self._script("logger", "#!/bin/bash\ncat >/dev/null 2>&1 &\n")
        self._script("op", FAKE_OP)
        self.state = self.tmp / "state"
        self.lock = self.state / "1p-killswitch"
        self.op_calls = self.tmp / "op-calls"
        self.op_calls.touch()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp),
            "OP_KILLSWITCH_STATE_DIR": str(self.state),
            "OP_KILLSWITCH_METRIC_FILE": str(self.tmp / "textfiles" / "killswitch.prom"),
            "OP_SECRET_CACHE_DIR": str(self.tmp / "secrets"),
            "OP_FAKE_CALLS": str(self.op_calls),
        }

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def bash(self, script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(
            [str(self.bin / "bash"), "-c", script, "test", *args],
            env=full_env, capture_output=True, text=True, timeout=30, check=False,
        )

    def scan_playbook_output(self, content: str | bytes) -> int:
        log = self.tmp / "playbook.log"
        if isinstance(content, bytes):
            log.write_bytes(content)
        else:
            log.write_text(content)
        proc = self.bash(
            f'source "{LIB}"; op_killswitch_scan_playbook_output "$1"; echo "rc=$?"',
            str(log),
        )
        match = re.search(r"rc=(\d+)", proc.stdout)
        self.assertIsNotNone(match, proc.stderr)
        return int(match.group(1))

    def assertTrips(self, content: str | bytes) -> None:
        self.assertEqual(self.scan_playbook_output(content), 0, "scanner did not report a trip")
        self.assertTrue(self.lock.exists(), "kill switch lock was not created")
        self.assertIn("trip_reason=rate_limited", self.lock.read_text())

    def assertDoesNotTrip(self, content: str | bytes) -> None:
        # rc 1 (not merely "no lock"), so a missing scanner fails the test.
        self.assertEqual(self.scan_playbook_output(content), 1)
        self.assertFalse(self.lock.exists(), "kill switch tripped")


class PlaybookOutputFalsePositiveTests(KillSwitchSandbox):
    def test_incident_selfdiff_does_not_trip(self) -> None:
        text = FIXTURE.read_text()
        # Non-vacuous: the fixture really carries the phrase the old scan hit.
        self.assertEqual(len(re.findall(r"Too many requests", text)), 2)
        self.assertDoesNotTrip(text)

    def test_phrase_inside_a_diff_hunk_does_not_trip(self) -> None:
        self.assertDoesNotTrip(
            "TASK [example : Install notes] ************************************\n"
            "--- before: /etc/notes.txt\n"
            "+++ after: /repo/files/notes.txt\n"
            "@@ -1,3 +1,4 @@\n"
            " # 1Password answers Too many requests when the quota is gone.\n"
            "-# old: rate limited, rate-limited, 429 Too Many\n"
            "+# new: rate limited, rate-limited, 429 Too Many\n"
            "changed: [command-center1]\n"
        )

    def test_real_op_error_line_quoted_in_a_diff_does_not_trip(self) -> None:
        # A runbook or fixture deployed with --diff that pastes a real line.
        self.assertDoesNotTrip(
            "--- before\n"
            "+++ after: /usr/local/share/doc/op-runbook.txt\n"
            "@@ -0,0 +1,2 @@\n"
            f"+{OP_HOURLY}\n"
            f"-{OP_CLIENT_THROTTLED}\n"
        )

    def test_phrase_in_comments_task_names_and_debug_output_does_not_trip(self) -> None:
        self.assertDoesNotTrip(
            "TASK [op : Trip the kill switch on Too many requests] ****************\n"
            "ok: [command-center1] => {\n"
            '    "msg": "# the kill switch trips once op is rate limited (429 Too Many)"\n'
            "}\n"
            "# Too many requests\n"
            "[ERROR] the op call was rate-limited: Too many requests\n"
        )

    def test_killswitch_library_source_does_not_trip_itself(self) -> None:
        # The library is deployed with --diff too; it must never match itself,
        # whether shown as added lines, as unchanged context lines, or plain.
        text = LIB.read_text()
        self.assertDoesNotTrip(text)
        self.assertDoesNotTrip("".join(f"+{ln}\n" for ln in text.splitlines()))
        self.assertDoesNotTrip("".join(f" {ln}\n" for ln in text.splitlines()))

    def test_rate_limit_text_from_other_tools_does_not_trip(self) -> None:
        self.assertDoesNotTrip(
            # Vector on command-center1, journal 2026-09-12.
            "vector::sinks::util::retries: Retrying after error. error=Server "
            "responded with an error: 429 Too Many Requests\n"
            # ansible.builtin.uri / get_url against a throttled API.
            'fatal: [command-center1]: FAILED! => {"msg": "Status code was 429 and '
            'not [200]: HTTP Error 429: Too Many Requests", "status": 429}\n'
            "toomanyrequests: You have reached your pull rate limit.\n"
            "gh: API rate limit exceeded for user. You are rate limited.\n"
            "[ERROR] 2026-09-13T20:46:05.123Z [github] Too many requests\n"
        )


class PlaybookOutputDetectionTests(KillSwitchSandbox):
    def test_documented_hourly_limit_error_trips(self) -> None:
        self.assertTrips(f"{OP_HOURLY}\n")

    def test_documented_daily_limit_error_trips(self) -> None:
        self.assertTrips(f"{OP_DAILY}\n")

    def test_client_throttle_error_from_op_binary_trips(self) -> None:
        self.assertTrips(f"{OP_CLIENT_THROTTLED}\n")

    def test_op_error_inside_a_failed_task_result_trips(self) -> None:
        self.assertTrips(ansible_fatal(OP_HOURLY))

    def test_op_error_in_yaml_callback_stderr_lines_trips(self) -> None:
        self.assertTrips(
            "fatal: [command-center1]: FAILED! =>\n"
            "  changed: true\n"
            "  rc: 1\n"
            "  stderr_lines:\n"
            f"  - '{OP_DAILY.replace(chr(39), chr(39) * 2)}'\n"
        )

    def test_op_error_after_a_harmless_diff_still_trips(self) -> None:
        self.assertTrips(FIXTURE.read_text() + ansible_fatal(OP_CLIENT_THROTTLED))

    def test_op_error_after_invalid_utf8_still_trips(self) -> None:
        self.assertTrips(b"\xff\xfe garbage " + OP_HOURLY.encode() + b"\n")

    def test_missing_file_does_not_trip(self) -> None:
        proc = self.bash(
            f'source "{LIB}"; op_killswitch_scan_playbook_output "$1"; echo "rc=$?"',
            str(self.tmp / "absent.log"),
        )
        self.assertRegex(proc.stdout, r"^rc=1$")
        self.assertFalse(self.lock.exists())


class OpOutputScanStaysBroadTests(KillSwitchSandbox):
    """Where the input is op's own output, detection is not narrowed."""

    def test_scan_file_trips_on_bare_phrase_in_op_stderr(self) -> None:
        err = self.tmp / "op.err"
        for text in ("Too many requests", "you are being rate limited", "HTTP 429 Too Many Requests"):
            with self.subTest(text=text):
                self.lock.unlink(missing_ok=True)
                err.write_text(text + "\n")
                proc = self.bash(f'source "{LIB}"; op_killswitch_scan_file "$1"; echo "rc=$?"', str(err))
                self.assertRegex(proc.stdout, r"^rc=0$")
                self.assertTrue(self.lock.exists())

    def test_cached_op_read_trips_on_op_rate_limit_stderr(self) -> None:
        for stderr in (OP_HOURLY, "Too many requests. Please retry in  seconds"):
            with self.subTest(stderr=stderr):
                self.lock.unlink(missing_ok=True)
                proc = self.bash(
                    f'source "{LIB}"; source "{CACHE_LIB}"; '
                    'cached_op_read some_slug "op://Vault/Item/field"; echo "rc=$?"',
                    env={"OP_SERVICE_ACCOUNT_TOKEN": "dummy", "OP_FAKE_STDERR": stderr},
                )
                self.assertRegex(proc.stdout, r"^rc=1$", proc.stderr)
                self.assertIn("read op://Vault/Item/field", self.op_calls.read_text())
                self.assertTrue(self.lock.exists(), "cached_op_read did not trip the kill switch")


class WrapperWiringTests(unittest.TestCase):
    """The timer wrappers write to fixed /var paths and cannot run sandboxed,
    so check which scanner they hand the playbook log to."""

    def test_wrappers_scan_playbook_logs_with_the_anchored_scanner(self) -> None:
        for rel in ("scripts/run-proxmox.sh", "scripts/run-security.sh", "scripts/run-cmd-center.sh"):
            with self.subTest(wrapper=rel):
                text = (REPO / rel).read_text()
                self.assertNotRegex(text, r'op_killswitch_scan_file\s+"\$tmpfile"')
                self.assertRegex(text, r'\n\s*op_killswitch_scan_playbook_output "\$tmpfile" \|\| true\n')

    def test_collector_role_ships_the_same_library(self) -> None:
        # The role's file is a symlink; a stale copy would reintroduce #160.
        self.assertEqual(ROLE_LIB.resolve(), LIB.resolve())


if __name__ == "__main__":
    unittest.main()
