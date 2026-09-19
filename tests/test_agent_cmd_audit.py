"""Tests for roles/cmd_center/files/agent-cmd-audit.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 pytest tests/test_agent_cmd_audit.py -rs

SAFETY: these tests must never reach Discord or 1Password. Every test that
touches the posting path injects a FakeOpener; a guard test asserts that the
real urlopen is never used, and the leak tests assert the webhook string never
reaches a log, an exception or the metrics file.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "roles" / "cmd_center" / "files" / "agent-cmd-audit"

FAKE_WEBHOOK = "https://discord.com/api/webhooks/1550680054516940803/" + "x" * 60


def load_module():
    loader = importlib.machinery.SourceFileLoader("agent_cmd_audit", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


aca = load_module()


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def http_error(code: int, body: dict | None = None, headers: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {}).encode("utf-8")
    return urllib.error.HTTPError(
        "https://discord.invalid/api/webhooks/redacted",
        code,
        f"HTTP {code}",
        headers or {},
        io.BytesIO(payload),
    )


class FakeOpener:
    """Callable stand-in for urlopen that can be primed with failures."""

    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.failures = list(failures or [])
        self.requests: list = []

    def __call__(self, request, timeout=None):
        assert timeout is not None and timeout > 0, "every request must carry a positive timeout"
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return FakeResponse()

    def bodies(self) -> list[dict]:
        return [json.loads(r.data.decode("utf-8")) for r in self.requests]


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, secs: float) -> None:
        self.calls.append(secs)


class Sandbox:
    """A temp state dir plus a Config pointed at it."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.prom = self.root / "metrics" / "agent_cmd_audit.prom"
        env = {
            "AGENT_CMD_AUDIT_CONFIG": str(self.root / "missing-config"),
            "AGENT_CMD_AUDIT_STATE_DIR": str(self.state_dir),
            "AGENT_CMD_AUDIT_PROM_FILE": str(self.prom),
            "AGENT_CMD_AUDIT_HOST": "test-host",
            "AGENT_CMD_AUDIT_OP_REFERENCE": "op://Infrastructure/fake/webhook_url",
            "AGENT_CMD_AUDIT_HERDR_BIN": "",
        }
        self.cfg = aca.Config(env)

    def close(self) -> None:
        self.tmp.cleanup()


class SanitizeTests(unittest.TestCase):
    """The redaction table. Every row must also survive the fail-closed check."""

    LEAKY = [
        ('curl -H "Authorization: Bearer ghp_AAAABBBBCCCCDDDDEEEEFFFFGG1234" https://x.invalid', "ghp_"),
        ("GITHUB_TOKEN=ghp_AAAABBBBCCCCDDDDEEEEFFFFGG1234 gh pr list", "ghp_"),
        ("export ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-ant-"),
        ("kubectl create secret generic x --from-literal=password=hunter2", "hunter2"),
        ("curl -u admin:hunter2 https://npm.invalid", "hunter2"),
        ("aws s3 ls --profile x # AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef", "eyJhbGciOiJIUzI1NiJ9"),
        ("slack-post xoxb-1111111111-2222222222-abcdefghijkl", "xoxb-"),
        ("psql postgres://user:hunter2@db.invalid/app", "hunter2"),
    ]

    SAFE = [
        "git log --oneline -20",
        # Regression: the old bare -p password rule rewrote this to --p[redacted].
        "git status --porcelain",
        "ps -p 12345 -o comm=",
        "grep -Pn 'token' README.md",
        "curl -s --path-as-is https://example.invalid/a",
        "kubectl get pods -n automation",
        "sha256sum /usr/local/bin/agent-cmd-audit",
        "ls -la /var/lib/node_exporter/textfiles",
        "systemctl --user is-active herdr.service",
        "grep -rn 'discord' roles/ | head -20",
    ]

    def test_known_secrets_never_survive(self):
        for command, canary in self.LEAKY:
            with self.subTest(command=command):
                text, _ = aca.sanitize_command(command)
                self.assertNotIn(canary, text)
                self.assertIsNone(
                    aca.SECRETLIKE_RE.search(text),
                    f"output still matches the secret filter: {text!r}",
                )

    def test_safe_commands_survive_intact(self):
        for command in self.SAFE:
            with self.subTest(command=command):
                text, reason = aca.sanitize_command(command)
                self.assertEqual(reason, "")
                # Byte-for-byte, not just "the program name survived": a
                # weaker assertion here passed while the old -p rule was
                # rewriting `git status --porcelain` to `--p[redacted]`.
                self.assertEqual(text, command)

    def test_sensitive_programs_drop_their_arguments(self):
        text, reason = aca.sanitize_command("op read op://Infrastructure/Foo/password")
        self.assertEqual(reason, "sensitive_program")
        self.assertIn("op", text)
        self.assertNotIn("Infrastructure", text)

    def test_heredoc_body_is_withheld(self):
        text, reason = aca.sanitize_command("cat <<'EOF' > /tmp/x\n-----BEGIN PRIVATE KEY-----\nEOF")
        self.assertIn(reason, ("heredoc", "private_key"))
        self.assertNotIn("BEGIN PRIVATE KEY", text)

    def test_heredoc_keeps_the_command_around_the_body(self):
        # The first line is the part worth auditing: the program, its flags and
        # where the output went. Withholding it too was the original bug.
        text, reason = aca.sanitize_command(
            "cat <<'EOF' > /etc/nginx/conf.d/foo.conf\nserver { listen 80; }\nEOF\nnginx -t"
        )
        self.assertEqual(reason, "heredoc")
        self.assertIn("/etc/nginx/conf.d/foo.conf", text)
        self.assertIn("nginx -t", text)
        self.assertIn(aca.WITHHELD_HEREDOC, text)
        self.assertNotIn("listen 80", text)

    def test_heredoc_body_never_survives_any_introducer_form(self):
        bodies = {
            "quoted": "cat <<'EOF'\nLEAKED\nEOF",
            "double quoted": 'cat <<"EOF"\nLEAKED\nEOF',
            "bare": "cat <<EOF\nLEAKED\nEOF",
            "escaped": "cat <<\\EOF\nLEAKED\nEOF",
            "spaced": "cat << EOF\nLEAKED\nEOF",
            "tab stripped": "cat <<-EOF\n\tLEAKED\n\tEOF",
            "second heredoc": "join <<A <<B\nfirst\nA\nLEAKED\nB",
            "later line": "set -e\ncat <<EOF\nLEAKED\nEOF",
        }
        for name, command in bodies.items():
            with self.subTest(form=name):
                text, reason = aca.sanitize_command(command)
                self.assertEqual(reason, "heredoc")
                self.assertNotIn("LEAKED", text)

    def test_unterminated_heredoc_swallows_the_rest(self):
        # No terminator means we cannot tell body from command, so everything
        # after the introducer goes rather than being treated as command text.
        text, reason = aca.sanitize_command("cat <<EOF\nLEAKED\nrm -rf /tmp/x")
        self.assertEqual(reason, "heredoc")
        self.assertNotIn("LEAKED", text)
        self.assertNotIn("rm -rf", text)
        self.assertIn("cat <<EOF", text)

    def test_herestring_is_not_treated_as_a_heredoc(self):
        # A herestring is one line, so the ordinary redactions apply to it.
        command = "grep -c x <<< 'hello'"
        text, reason = aca.sanitize_command(command)
        self.assertEqual(reason, "")
        self.assertEqual(text, command)

    def test_herestring_secrets_still_fail_closed(self):
        text, reason = aca.sanitize_command("post <<< 'token: abcdefghijklmnop'")
        self.assertEqual(reason, "fail_closed")
        self.assertNotIn("abcdefghijklmnop", text)

    def test_big_heredoc_is_kept_once_its_body_is_gone(self):
        # Size is judged on what would be posted. Before the body was stripped
        # first, a large heredoc lost its first line to the oversize rule.
        command = "python3 - <<'PY'\n" + "x" * (aca.HARD_DROP_CHARS + 10) + "\nPY"
        text, reason = aca.sanitize_command(command)
        self.assertEqual(reason, "heredoc")
        self.assertIn("python3", text)
        self.assertNotIn("xxxx", text)

    def test_program_name_skips_only_real_assignments(self):
        cases = {
            # The regression: "=" is not in the last path segment, so the old
            # rule kept the token and called the program `e2e2;`.
            "E=/tmp/scratch/e2e2; cd $E; docker ps": "cd",
            "M=/home/ladino/memory/project.md python3 -": "python3",
            "TZ=UTC date": "date",
            "/usr/local/bin/agent-cmd-audit record": "agent-cmd-audit",
            "sudo -n systemctl restart x": "sudo",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(aca.program_name(command), expected)

    def test_oversize_command_is_withheld(self):
        text, reason = aca.sanitize_command("echo " + "a" * (aca.HARD_DROP_CHARS + 10))
        self.assertEqual(reason, "oversize")
        self.assertIn(aca.WITHHELD_OVERSIZE, text)

    def test_fail_closed_on_unknown_secret_shape(self):
        # A pattern the substitutions do not rewrite must be withheld, not posted.
        text, reason = aca.sanitize_command("deploy --config token: abcdefghijklmnop")
        self.assertEqual(reason, "fail_closed")
        self.assertIn(aca.WITHHELD_SECRET, text)

    def test_registered_webhook_is_redacted(self):
        aca.register_secret(FAKE_WEBHOOK)
        try:
            text, _ = aca.sanitize_command(f"curl -X POST {FAKE_WEBHOOK}")
            self.assertNotIn(FAKE_WEBHOOK, text)
        finally:
            aca._SECRETS.discard(FAKE_WEBHOOK)


class DiscordTextTests(unittest.TestCase):
    def test_markdown_and_mentions_are_defanged(self):
        out = aca.discord_text("@everyone [click](http://evil.invalid) *bold*")
        self.assertNotIn("@everyone", out)
        self.assertNotIn("](", out)
        # The scheme is defanged (and then escaped), so nothing auto-links.
        self.assertNotIn("http://", out)
        self.assertIn("evil.invalid", out)

    def test_cap_never_cuts_an_escape_in_half(self):
        out = aca.discord_text("*" * 200, 50)
        self.assertLessEqual(len(out.replace("\\", "")), 51)
        self.assertFalse(out.endswith("\\"))


class RecordShapeTests(unittest.TestCase):
    """Guards for the 2.1.273 payload shape, verified against real transcripts."""

    def test_success_has_no_exit_code_field(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "abc123",
            "tool_input": {"command": "git status"},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
        }
        record = aca.build_record(payload, {}, aca.utcnow())
        self.assertEqual(record["status"], aca.STATUS_OK)
        self.assertNotIn("exit_code", record)

    def test_failure_exit_code_comes_from_the_error_string(self):
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "session_id": "abc123",
            "tool_input": {"command": "sh -c 'exit 3'"},
            "error": "Error: Exit code 3",
        }
        record = aca.build_record(payload, {}, aca.utcnow())
        self.assertEqual(record["status"], aca.STATUS_FAILED)
        self.assertEqual(record["exit_code"], 3)

    def test_reworded_error_degrades_to_unknown_code(self):
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "session_id": "abc",
            "tool_input": {"command": "false"},
            "error": "the command did not succeed",
        }
        record = aca.build_record(payload, {}, aca.utcnow())
        self.assertEqual(record["status"], aca.STATUS_FAILED)
        self.assertNotIn("exit_code", record)
        self.assertEqual(aca.status_word(record), "failed")

    def test_backgrounded_is_labelled_outcome_unknown(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "abc",
            "tool_input": {"command": "npm run dev", "run_in_background": True},
            "tool_response": {"backgroundTaskId": "t1"},
        }
        record = aca.build_record(payload, {}, aca.utcnow())
        self.assertEqual(record["status"], aca.STATUS_BACKGROUNDED)
        self.assertIn("backgrounded", aca.transcript_entry(record))

    def test_herdr_env_is_captured(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "abc",
            "tool_input": {"command": "ls"},
            "tool_response": {},
        }
        env = {"HERDR_WORKSPACE_ID": "w5", "HERDR_PANE_ID": "w5:p1"}
        record = aca.build_record(payload, env, aca.utcnow())
        self.assertEqual(record["workspace"], "w5")
        self.assertEqual(record["pane"], "w5:p1")


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def record(self, command: str) -> dict:
        return {
            "ts": aca.iso(aca.utcnow()),
            "event": "PostToolUse",
            "status": aca.STATUS_OK,
            "command": command,
            "session_id": "s1",
            "workspace": "w1",
            "pane": "w1:p1",
            "host": "test-host",
            "agent": "claude",
        }

    def test_append_and_seal_round_trip(self):
        self.assertEqual(aca.append_record(self.box.state_dir, self.record("ls")), "")
        sealed = aca.seal_spool(self.box.state_dir)
        self.assertIsNotNone(sealed)
        records, bad = aca.read_batch(sealed)
        self.assertEqual(bad, 0)
        self.assertEqual(records[0]["command"], "ls")
        self.assertFalse((self.box.state_dir / "current.jsonl").exists())

    def test_spool_file_is_private(self):
        aca.append_record(self.box.state_dir, self.record("ls"))
        mode = (self.box.state_dir / "current.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_writer_refuses_once_the_spool_is_full(self):
        big = self.record("x" * 100)
        current = self.box.state_dir / "current.jsonl"
        self.box.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        current.write_bytes(b"a" * (aca.MAX_CURRENT_BYTES + 1))
        self.assertEqual(aca.append_record(self.box.state_dir, big), "spool_full")

    def test_oversize_record_is_truncated_not_dropped(self):
        self.assertEqual(aca.append_record(self.box.state_dir, self.record("y" * 9000)), "")
        sealed = aca.seal_spool(self.box.state_dir)
        records, bad = aca.read_batch(sealed)
        self.assertEqual(bad, 0)
        self.assertTrue(records[0]["truncated_at_record"])

    def test_concurrent_writers_do_not_interleave(self):
        env = dict(os.environ)
        env.update({
            "AGENT_CMD_AUDIT_CONFIG": str(self.box.root / "missing-config"),
            "AGENT_CMD_AUDIT_STATE_DIR": str(self.box.state_dir),
            "AGENT_CMD_AUDIT_PROM_FILE": str(self.box.prom),
        })
        payload = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_input": {"command": "echo " + "z" * 200},
            "tool_response": {},
        })
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "record"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            )
            for _ in range(30)
        ]
        for proc in procs:
            proc.communicate(payload.encode("utf-8"), timeout=60)
            self.assertEqual(proc.returncode, 0)
        sealed = aca.seal_spool(self.box.state_dir)
        records, bad = aca.read_batch(sealed)
        self.assertEqual(bad, 0, "a line was interleaved or truncated")
        self.assertEqual(len(records), 30)

    def test_off_file_stops_recording(self):
        self.box.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.box.state_dir / "OFF").write_text("")
        self.assertFalse(self.box.cfg.recording_enabled())

    def test_backlog_drops_the_oldest_batch(self):
        inbox = self.box.state_dir / "inbox"
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        for i in range(aca.MAX_INBOX_FILES + 5):
            (inbox / f"{i:020d}.jsonl").write_text('{"command":"ls"}\n')
        dropped = aca.trim_inbox(self.box.state_dir)
        self.assertEqual(dropped, 5)
        remaining = [p.name for p in aca.inbox_batches(self.box.state_dir)]
        self.assertEqual(len(remaining), aca.MAX_INBOX_FILES)
        self.assertNotIn(f"{0:020d}.jsonl", remaining)


class PostTests(unittest.TestCase):
    def test_rate_limit_body_beats_header_and_retries(self):
        opener = FakeOpener([
            http_error(429, body={"retry_after": 2.5}),
            http_error(429, headers={"Retry-After": "1"}),
            http_error(500),
        ])
        sleeper = RecordingSleeper()
        aca.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=sleeper)
        self.assertEqual(sleeper.calls, [2.5, 1.0, 1.0])
        self.assertEqual(len(opener.requests), 4)

    def test_persistent_rate_limit_raises(self):
        opener = FakeOpener([http_error(429, body={"retry_after": 1}) for _ in range(aca.POST_MAX_ATTEMPTS)])
        with self.assertRaises(aca.PostError):
            aca.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=RecordingSleeper())

    def test_client_error_is_not_retried(self):
        opener = FakeOpener([http_error(404)])
        with self.assertRaises(aca.PostError):
            aca.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=RecordingSleeper())
        self.assertEqual(len(opener.requests), 1)

    def test_wait_true_is_requested_so_a_2xx_proves_acceptance(self):
        opener = FakeOpener()
        aca.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=RecordingSleeper())
        self.assertTrue(opener.requests[0].full_url.endswith("?wait=true"))

    def test_webhook_never_appears_in_an_exception(self):
        aca.register_secret(FAKE_WEBHOOK)
        try:
            # A transport error is retryable, so every attempt must fail for
            # post_payload to give up and raise.
            opener = FakeOpener([
                OSError(f"connection to {FAKE_WEBHOOK} refused")
                for _ in range(aca.POST_MAX_ATTEMPTS)
            ])
            with self.assertRaises(aca.PostError) as ctx:
                aca.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=RecordingSleeper())
            self.assertNotIn(FAKE_WEBHOOK, str(ctx.exception))
        finally:
            aca._SECRETS.discard(FAKE_WEBHOOK)

    def test_webhook_never_appears_in_a_log_record(self):
        aca.register_secret(FAKE_WEBHOOK)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(aca.RedactingFormatter("%(message)s"))
        aca.log.handlers.clear()
        aca.log.addHandler(handler)
        try:
            aca.log.warning("posting to %s failed", FAKE_WEBHOOK)
            self.assertNotIn(FAKE_WEBHOOK, stream.getvalue())
            self.assertIn("[redacted", stream.getvalue())
        finally:
            aca._SECRETS.discard(FAKE_WEBHOOK)
            aca.log.handlers.clear()


class PayloadTests(unittest.TestCase):
    def make(self, n: int, session: str = "s1") -> list[dict]:
        return [
            {
                "ts": "2026-09-19T02:25:41Z",
                "status": aca.STATUS_OK,
                "command": f"echo {i}",
                "session_id": session,
                "workspace": "w1",
                "pane": "w1:p1",
                "host": "test-host",
                "agent": "claude",
            }
            for i in range(n)
        ]

    def test_payload_suppresses_mentions(self):
        payload, _, _ = aca.build_payload(self.make(1), {})
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_withheld_commands_are_counted(self):
        records = self.make(1)
        records[0]["command"] = "op read op://Infrastructure/x/password"
        payload, withheld, _ = aca.build_payload(records, {})
        self.assertEqual(withheld, 1)
        self.assertIn("1 withheld", payload["content"])

    def test_summary_names_the_sessions_and_counts(self):
        records = self.make(3, "a") + self.make(2, "b")
        payload, _, _ = aca.build_payload(records, {"w1": "Main Driver"})
        self.assertIn("5 commands", payload["content"])
        self.assertIn("Main Driver", payload["content"])
        self.assertNotIn("embeds", payload)

    def test_summary_fits_the_content_limit(self):
        records = [
            dict(r, command="x" * 400, session_id=f"s{i % 12}", workspace=f"w{i}")
            for i, r in enumerate(self.make(120, "s0"))
        ]
        payload, _, _ = aca.build_payload(records, {})
        self.assertLessEqual(len(payload["content"]), aca.CONTENT_MAX)

    def test_failures_are_called_out_with_a_snippet(self):
        records = self.make(2)
        records[1]["status"] = aca.STATUS_FAILED
        records[1]["command"] = "kubectl -n media rollout status deploy/sonarr"
        payload, _, _ = aca.build_payload(records, {})
        self.assertIn("failed", payload["content"])
        self.assertIn("rollout status deploy/sonarr", payload["content"])

    def test_snippet_is_not_markdown_escaped(self):
        # Escaping inside a code span is what put `list\-timers` in the feed.
        records = self.make(1)
        records[0]["status"] = aca.STATUS_FAILED
        records[0]["command"] = "systemctl list-timers 'agent-cmd-audit*' --no-pager"
        payload, _, _ = aca.build_payload(records, {})
        self.assertIn("list-timers 'agent-cmd-audit*' --no-pager", payload["content"])
        self.assertNotIn("\\-", payload["content"])

    def test_snippet_cannot_escape_its_code_span_or_ping(self):
        records = self.make(1)
        records[0]["status"] = aca.STATUS_FAILED
        records[0]["command"] = "echo '`@everyone`'"
        payload, _, _ = aca.build_payload(records, {})
        self.assertNotIn("@everyone", payload["content"])
        self.assertEqual(payload["content"].count("`"), 2)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_canary_is_summarised_not_counted_as_a_command(self):
        records = self.make(1)
        records.append(dict(records[0], canary=True, nonce="abc123", command=""))
        payload, _, _ = aca.build_payload(records, {})
        self.assertIn("1 command ", payload["content"] + " ")
        self.assertIn("canary", payload["content"])

    def test_file_is_always_attached(self):
        _, _, transcript = aca.build_payload(self.make(2), {})
        self.assertTrue(transcript)
        self.assertIn(b"echo 0", transcript)

    def test_file_holds_the_full_command(self):
        records = self.make(1)
        long_command = "deploy " + "abcdefgh " * 60
        records[0]["command"] = long_command
        payload, _, transcript = aca.build_payload(records, {}, host="test-host")
        text = transcript.decode("utf-8")
        self.assertIn(long_command.strip(), text)
        self.assertIn("test-host", text)
        # The message points at the file rather than repeating the command.
        self.assertNotIn("abcdefgh abcdefgh", payload["content"])

    def test_subagent_commands_get_their_own_block(self):
        # A subagent shares the parent's session id; its commands are still
        # its own, so they must not be filed under the parent's heading.
        records = self.make(2)
        records[1]["agent_type"] = "kuma-agent"
        _, _, transcript = aca.build_payload(records, {})
        text = transcript.decode("utf-8")
        self.assertEqual(text.count("## "), 2)
        self.assertIn("subagent kuma-agent", text)

    def test_file_does_not_lift_redaction(self):
        records = self.make(3)
        records[0]["command"] = "cat <<'EOF' > /tmp/x\n" + "LEAKED " * 80 + "\nEOF"
        records[1]["command"] = "deploy --token " + "z" * 400
        records[2]["command"] = "rsync -av " + "/srv/data/dir " * 40
        _, _, transcript = aca.build_payload(records, {})
        text = transcript.decode("utf-8")
        self.assertNotIn("LEAKED", text)
        self.assertNotIn("z" * 40, text)
        self.assertIn("[redacted]", text)

    def test_file_groups_by_session_and_shows_exit_codes(self):
        records = self.make(1, "a") + self.make(1, "b")
        records[1]["status"] = aca.STATUS_FAILED
        records[1]["exit_code"] = 3
        _, _, transcript = aca.build_payload(records, {"w1": "Main Driver"})
        text = transcript.decode("utf-8")
        self.assertEqual(text.count("## "), 2)
        self.assertIn("session a", text)
        self.assertIn("failed(3)", text)

    def test_attachment_is_capped(self):
        records = [
            dict(r, command="echo " + "y" * 900, session_id="s0")
            for r in self.make(400, "s0")
        ]
        _, _, transcript = aca.build_payload(records, {})
        self.assertIsNotNone(transcript)
        self.assertLessEqual(len(transcript), aca.TRANSCRIPT_MAX_BYTES)


class MultipartTests(unittest.TestCase):
    def post(self, attachment: bytes | None):
        opener = FakeOpener()
        aca.post_payload(
            FAKE_WEBHOOK, {"embeds": []}, opener=opener, sleep=RecordingSleeper(), attachment=attachment
        )
        return opener.requests[0]

    def test_plain_batch_still_posts_json(self):
        request = self.post(None)
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"embeds": []})

    def test_attachment_is_sent_as_multipart(self):
        request = self.post(b"21:39:02  ok  echo hello\n")
        content_type = request.headers["Content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        body = request.data
        self.assertIn(b'name="payload_json"', body)
        self.assertIn(b'name="files[0]"; filename="commands.txt"', body)
        self.assertIn(b"echo hello", body)
        # Framing: opens with the boundary and closes with the terminator.
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_each_post_gets_a_fresh_boundary(self):
        first = self.post(b"a")
        second = self.post(b"b")
        self.assertNotEqual(first.headers["Content-type"], second.headers["Content-type"])

    def test_multipart_body_never_holds_the_webhook(self):
        request = self.post(b"echo hello\n")
        self.assertNotIn(FAKE_WEBHOOK.encode(), request.data)


class FlushTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def spool(self, command: str = "git status") -> None:
        aca.append_record(self.box.state_dir, {
            "ts": aca.iso(aca.utcnow()),
            "status": aca.STATUS_OK,
            "command": command,
            "session_id": "s1",
            "workspace": "w1",
            "pane": "w1:p1",
            "host": "test-host",
            "agent": "claude",
        })

    def test_dry_run_fetches_no_secret_and_keeps_the_batch(self):
        self.spool()
        calls = []
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: calls.append(a) or FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            stdout, sys.stdout = sys.stdout, buf
            try:
                aca.flush_once(self.box.cfg, dry_run=True)
            finally:
                sys.stdout = stdout
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertEqual(calls, [], "a dry run must never fetch the secret")
        self.assertIn("content", buf.getvalue())
        self.assertIn(aca.TRANSCRIPT_FILENAME, buf.getvalue())
        self.assertEqual(len(aca.inbox_batches(self.box.state_dir)), 1, "a dry run must keep the batch")

    def test_successful_flush_deletes_the_batch_and_stamps_success(self):
        self.spool()
        opener = FakeOpener()
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            state = aca.flush_once(self.box.cfg, dry_run=False, opener=opener, sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(aca.inbox_batches(self.box.state_dir), [])
        self.assertGreater(state["last_success"], 0)
        self.assertEqual(state["posted_total"], 1)

    def test_failed_post_keeps_the_batch_and_still_exits_zero(self):
        self.spool()
        opener = FakeOpener([http_error(500) for _ in range(aca.POST_MAX_ATTEMPTS)])
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            state = aca.flush_once(self.box.cfg, dry_run=False, opener=opener, sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertEqual(len(aca.inbox_batches(self.box.state_dir)), 1, "a failed post must not lose the batch")
        self.assertEqual(state["post_failures_total"], 1)
        self.assertNotIn("last_success", state)

    def test_no_webhook_holds_the_batch_and_counts(self):
        self.spool()
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: None  # type: ignore[assignment]
        try:
            state = aca.flush_once(self.box.cfg, dry_run=False, opener=FakeOpener(), sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertEqual(state["no_webhook_total"], 1)
        self.assertEqual(len(aca.inbox_batches(self.box.state_dir)), 1)

    def test_idle_flush_touches_no_secret_and_writes_metrics(self):
        calls = []
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: calls.append(a) or FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            aca.flush_once(self.box.cfg, dry_run=False, opener=FakeOpener(), sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertEqual(calls, [], "an idle lab must not spend a 1Password lookup")
        self.assertTrue(self.box.prom.exists())

    def test_metrics_file_is_world_readable_and_complete(self):
        aca.flush_once(self.box.cfg, dry_run=False, opener=FakeOpener(), sleep=RecordingSleeper())
        self.assertEqual(self.box.prom.stat().st_mode & 0o777, 0o644)
        body = self.box.prom.read_text()
        for name, _, _ in aca.METRIC_HELP:
            self.assertIn(f"\n{name} ", "\n" + body)
        self.assertIn('agent_cmd_audit_dropped_total{reason="backlog"}', body)
        # No temp file left where node_exporter would glob it.
        self.assertEqual([p.name for p in self.box.prom.parent.glob("*.prom")], [self.box.prom.name])

    def test_canary_stamps_only_after_acceptance(self):
        aca.cmd_canary(None, self.box.cfg)
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            failed = aca.flush_once(
                self.box.cfg, dry_run=False,
                opener=FakeOpener([http_error(500) for _ in range(aca.POST_MAX_ATTEMPTS)]),
                sleep=RecordingSleeper(),
            )
            self.assertNotIn("canary_posted", failed)
            ok = aca.flush_once(self.box.cfg, dry_run=False, opener=FakeOpener(), sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
        self.assertGreater(ok["canary_posted"], 0)

    def test_webhook_never_reaches_the_metrics_file(self):
        self.spool()
        aca.register_secret(FAKE_WEBHOOK)
        original = aca.resolve_webhook
        aca.resolve_webhook = lambda *a, **k: FAKE_WEBHOOK  # type: ignore[assignment]
        try:
            aca.flush_once(self.box.cfg, dry_run=False, opener=FakeOpener(), sleep=RecordingSleeper())
        finally:
            aca.resolve_webhook = original  # type: ignore[assignment]
            aca._SECRETS.discard(FAKE_WEBHOOK)
        self.assertNotIn(FAKE_WEBHOOK, self.box.prom.read_text())
        self.assertNotIn(FAKE_WEBHOOK, (self.box.state_dir / "state.json").read_text())


class HookEntryTests(unittest.TestCase):
    """The recorder must never break a session, whatever it is fed."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def run_record(self, payload: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({
            "AGENT_CMD_AUDIT_CONFIG": str(self.box.root / "missing-config"),
            "AGENT_CMD_AUDIT_STATE_DIR": str(self.box.state_dir),
            "AGENT_CMD_AUDIT_PROM_FILE": str(self.box.prom),
        })
        return subprocess.run(
            [sys.executable, str(SCRIPT), "record"],
            input=payload, text=True, capture_output=True, env=env, timeout=60,
        )

    def test_garbage_stdin_exits_zero_silently(self):
        for payload in ("", "not json", "[]", '{"tool_name":"Bash"}', '{"tool_name":"Read"}'):
            with self.subTest(payload=payload):
                proc = self.run_record(payload)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "", "the hook must never write to stdout")

    def test_non_bash_tools_are_ignored(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Read",
            "session_id": "s", "tool_input": {"file_path": "/etc/passwd"}, "tool_response": {},
        })
        self.assertEqual(self.run_record(payload).returncode, 0)
        self.assertFalse((self.box.state_dir / "current.jsonl").exists())

    def test_bash_command_is_recorded(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "s", "tool_input": {"command": "uptime"}, "tool_response": {},
        })
        self.assertEqual(self.run_record(payload).returncode, 0)
        body = (self.box.state_dir / "current.jsonl").read_text()
        self.assertIn("uptime", body)


if __name__ == "__main__":
    unittest.main()
