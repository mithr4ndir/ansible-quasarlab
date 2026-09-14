"""Tests for files/lab-changelog and tasks/changelog.yml.

Run from the repo root:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" pytest roles/cmd_center/tests -rs

SAFETY: nothing here reaches GitHub, Anthropic, 1Password or Discord.
- gh and claude are fake executables in a sandbox bin directory.
- The webhook "secret" is a fake URL seeded into a temporary secret cache and
  read back through the REAL scripts/lib/op-killswitch.sh and
  op-secret-cache.sh. Every shell gets PATH set to the sandbox, where `op` is a
  fake that only records its argv, so the real op in /usr/local/bin is not
  reachable (guard test below), and OP_SERVICE_ACCOUNT_TOKEN is removed.
- HTTP posts go to a fake opener in process, and to a sitecustomize shim that
  replaces urllib.request.urlopen in subprocess runs.
"""

from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
SCRIPT = ROLE / "files" / "lab-changelog"
LIB_DIR = REPO / "scripts" / "lib"
TASKS = ROLE / "tasks" / "changelog.yml"
MAIN = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"
HOST_VARS = REPO / "host_vars" / "command-center1" / "vars.yml"
TEMPLATES = ROLE / "templates"

FAKE_WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/FAKE-token_value-for-tests-only"
# Computed here, not by the script, so a wrong tag in the script is caught.
SLUG = "discord_changelog_webhook_url." + __import__("hashlib").sha256(
    b"op://Infrastructure/rmmf24ed3vvjffafar6wtah4ky/webhook_url").hexdigest()[:16]

TOOLS = ["awk", "bash", "cat", "chmod", "date", "dirname", "flock", "mkdir", "mktemp",
         "mv", "rm", "sh", "sleep", "stat", "timeout", "touch", "basename"]


# ---------------------------------------------------------------------------
# Loading the script
# ---------------------------------------------------------------------------


def load_module() -> Any:
    name = "lab_changelog_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def reset_logger() -> None:
    # The "lab-changelog" logger is process global, so handlers from one test
    # (a closed capture stream, another test's state dir) must not leak.
    import logging
    logger = logging.getLogger("lab-changelog")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


@pytest.fixture()
def lc() -> Any:
    reset_logger()
    module = load_module()
    module._SECRETS.clear()
    yield module
    reset_logger()


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


FAKE_OP = """#!/bin/bash
echo "$*" >> "$FAKE_OP_CALLS"
# Like the real op, ratelimit fails without a service account token.
if [[ "$1 $2" == "service-account ratelimit" && -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    echo "[ERROR] you must specify the service account" >&2
    exit 1
fi
if [[ "$1 $2" == "service-account ratelimit" && -n "${FAKE_OP_REMAINING:-}" ]]; then
    used=$((1000 - FAKE_OP_REMAINING))
    echo "TYPE       ACTION        LIMIT    USED    REMAINING    RESET"
    echo "account    read_write    1000     ${used}     ${FAKE_OP_REMAINING}           1 hour from now"
    exit 0
fi
if [[ "$1" == "read" ]]; then
    printf '%s' "${FAKE_OP_VALUE:-value-from-fake-op-read}"
    exit 0
fi
exit 1
"""

# Answers `gh <noun> list --repo R --state S ...` from $FAKE_GH_DATA, a JSON
# object keyed "noun:state:repo". Records every call.
FAKE_GH = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_GH_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
time.sleep(float(os.environ.get("FAKE_GH_SLEEP", "0")))
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("gh: simulated failure\\n")
    sys.exit(1)
def opt(name):
    return args[args.index(name) + 1]
data = json.load(open(os.environ["FAKE_GH_DATA"]))
print(json.dumps(data.get(f"{{args[0]}}:{{opt('--state')}}:{{opt('--repo')}}", [])))
"""

# Replaces urlopen in a subprocess run of the script. Records each request
# body; never opens a socket.
SITECUSTOMIZE = """
import io, json, os, urllib.request
_posts = os.environ.get("LC_TEST_POSTS")
if _posts:
    class _Resp(io.BytesIO):
        status = 200
    def _fake_urlopen(request, timeout=None):
        with open(_posts, "a") as handle:
            handle.write(json.dumps({"url": request.full_url, "body": json.loads(request.data)}) + "\\n")
        return _Resp(b"{}")
    urllib.request.urlopen = _fake_urlopen
"""


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.state = root / "state"
        self.cache = root / "secrets"
        self.cache.mkdir(mode=0o700)
        self.ks = root / "ks"
        self.ks.mkdir()
        self.op_calls = root / "op.calls"
        self.gh_calls = root / "gh.calls"
        self.gh_data = root / "gh.json"
        self.posts = root / "posts.jsonl"
        self.site = root / "site"
        self.site.mkdir()
        (self.site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found, f"{tool} is needed by the tests"
            (self.bin / tool).symlink_to(found)
        write_exe(self.bin / "logger", "#!/bin/sh\nexit 0\n")
        write_exe(self.bin / "op", FAKE_OP)
        write_exe(self.bin / "gh", FAKE_GH.format(python=sys.executable))
        self.gh_data.write_text("{}")
        self.op_calls.touch()
        self.gh_calls.touch()

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "LAB_CHANGELOG_STATE_DIR": str(self.state),
            "LAB_CHANGELOG_CONFIG": "",
            "LAB_CHANGELOG_OP_LIB_DIR": str(LIB_DIR),
            "LAB_CHANGELOG_OP_TOKEN_FILE": str(self.root / "no-token"),
            "LAB_CHANGELOG_CLAUDE_BIN": "",
            "LAB_CHANGELOG_REPOS": "mithr4ndir/ansible-quasarlab,mithr4ndir/k8s-argocd",
            "OP_SECRET_CACHE_DIR": str(self.cache),
            "OP_KILLSWITCH_STATE_DIR": str(self.ks),
            "OP_KILLSWITCH_METRIC_FILE": str(self.ks / "metric.prom"),
            "FAKE_OP_CALLS": str(self.op_calls),
            "FAKE_GH_CALLS": str(self.gh_calls),
            "FAKE_GH_DATA": str(self.gh_data),
            "PYTHONPATH": str(self.site),
            "LC_TEST_POSTS": str(self.posts),
        }
        env.update(extra)
        return env

    def seed_webhook(self, value: str = FAKE_WEBHOOK, age_secs: int = 0) -> None:
        path = self.cache / SLUG
        path.write_text(value)
        path.chmod(0o600)
        if age_secs:
            stamp = time.time() - age_secs
            os.utime(path, (stamp, stamp))

    def trip_killswitch(self) -> None:
        (self.ks / "1p-killswitch").write_text("tripped by test\n")

    def set_gh(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.gh_data.write_text(json.dumps(rows))

    def op_argv(self) -> list[str]:
        return self.op_calls.read_text().splitlines()

    def posted(self) -> list[dict[str, Any]]:
        if not self.posts.exists():
            return []
        return [json.loads(line) for line in self.posts.read_text().splitlines()]

    def log_text(self) -> str:
        path = self.state / "lab-changelog.log"
        return path.read_text() if path.exists() else ""


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = Sandbox(tmp_path)
    for key in list(os.environ):
        if key.startswith(("OP_", "LAB_CHANGELOG_")):
            monkeypatch.delenv(key)
    env = box.env()
    for key in ("PYTHONPATH", "LC_TEST_POSTS"):
        env.pop(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return box


def iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def gh_rows(when: dt.datetime) -> dict[str, list[dict[str, Any]]]:
    stamp = iso(when)
    return {
        "pr:merged:mithr4ndir/ansible-quasarlab": [
            {"number": 163, "title": "cmd_center: helm install idempotency", "url": "https://github.com/mithr4ndir/ansible-quasarlab/pull/163", "body": "b", "mergedAt": stamp},
        ],
        "issue:all:mithr4ndir/ansible-quasarlab": [
            {"number": 164, "title": "textfile directory is world-writable", "url": "https://github.com/mithr4ndir/ansible-quasarlab/issues/164", "body": "", "createdAt": stamp},
        ],
        "issue:closed:mithr4ndir/k8s-argocd": [
            {"number": 201, "title": "burn rate loses reads", "url": "https://github.com/mithr4ndir/k8s-argocd/issues/201", "body": "", "closedAt": stamp},
        ],
    }


def fake_runner(rows: dict[str, list[dict[str, Any]]], calls: list[list[str]] | None = None):
    def runner(argv: list[str], timeout: int) -> str:
        if calls is not None:
            calls.append(argv)
        key = f"{argv[1]}:{argv[argv.index('--state') + 1]}:{argv[argv.index('--repo') + 1]}"
        return json.dumps(rows.get(key, []))
    return runner


class FakeResponse(BytesIO):
    status = 200


class FakeOpener:
    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.failures = list(failures or [])
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        assert timeout is not None and timeout > 0, "every post needs a timeout"
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return FakeResponse(b"{}")

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(req.data) for req in self.requests]


def make_item(lc: Any, kind: str = "pr_merged", number: int = 1, title: str = "t", repo: str = "mithr4ndir/ansible-quasarlab") -> Any:
    return lc.Item(kind=kind, repo=repo, number=number, title=title, body="")


def config(lc: Any, box: Sandbox, **overrides: Any) -> Any:
    lc.setup_logging(box.state, to_stderr=False)
    cfg = lc.Config.from_env()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# Sandbox guard
# ---------------------------------------------------------------------------


def test_sandbox_cannot_reach_real_op(sandbox: Sandbox) -> None:
    proc = subprocess.run(["bash", "-c", "command -v op; echo token=${OP_SERVICE_ACCOUNT_TOKEN:-unset}"],
                          env=sandbox.env(), capture_output=True, text=True, check=True)
    assert proc.stdout.splitlines() == [str(sandbox.bin / "op"), "token=unset"]


# ---------------------------------------------------------------------------
# Window and hook input
# ---------------------------------------------------------------------------


def test_window_starts_at_first_transcript_timestamp(lc: Any, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "summary", "summary": "no timestamp here"}),
        "not json at all",
        json.dumps({"type": "user", "timestamp": "2026-09-13T10:00:00.123456789Z", "message": "secret-ish"}),
        json.dumps({"type": "assistant", "timestamp": "2026-09-13T09:00:00Z"}),
    ]) + "\n")
    start = lc.transcript_start(str(transcript))
    assert start == dt.datetime(2026, 9, 13, 10, 0, 0, 123456, tzinfo=dt.timezone.utc)


def test_window_offset_timestamp_is_normalized_to_utc(lc: Any, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"timestamp": "2026-09-13T12:00:00+02:00"}) + "\n")
    assert lc.transcript_start(str(transcript)) == dt.datetime(2026, 9, 13, 10, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("content", ["", "{}\n", json.dumps({"timestamp": "yesterday"}) + "\n"])
def test_window_without_timestamp_is_an_error(lc: Any, tmp_path: Path, content: str) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(content)
    with pytest.raises(lc.ChangelogError):
        lc.transcript_start(str(transcript))


def test_window_missing_transcript_is_an_error(lc: Any, tmp_path: Path) -> None:
    with pytest.raises(lc.ChangelogError):
        lc.transcript_start(str(tmp_path / "missing.jsonl"))


def test_hook_input_parsing(lc: Any) -> None:
    hook = lc.parse_hook_input(json.dumps({
        "session_id": "abc", "transcript_path": "/x/t.jsonl", "cwd": "/home/u/code", "reason": "exit",
        "hook_event_name": "SessionEnd",
    }).encode())
    assert (hook.session_id, hook.transcript_path, hook.cwd, hook.reason) == ("abc", "/x/t.jsonl", "/home/u/code", "exit")


def test_hook_input_non_string_fields_are_ignored(lc: Any) -> None:
    hook = lc.parse_hook_input('{"transcript_path": "/t", "cwd": ["x"], "reason": 5}')
    assert (hook.cwd, hook.reason, hook.session_id) == ("", "", "")


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"cwd": "/tmp"}', '{"transcript_path": 7}'])
def test_hook_input_rejects_bad_input(lc: Any, raw: str) -> None:
    with pytest.raises(lc.ChangelogError):
        lc.parse_hook_input(raw)


# ---------------------------------------------------------------------------
# Session-end: detached, silent, always exit 0
# ---------------------------------------------------------------------------


def transcript_at(box: Sandbox, start: dt.datetime) -> Path:
    path = box.root / "transcript.jsonl"
    path.write_text(json.dumps({"type": "user", "timestamp": iso(start)}) + "\n")
    return path


def run_session_end(box: Sandbox, stdin: str, *extra_args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "session-end", *extra_args],
        input=stdin,
        env=box.env(**env),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd or box.root),
    )


def hook_json(transcript: Path, cwd: Path) -> str:
    return json.dumps({"session_id": "s1", "transcript_path": str(transcript), "cwd": str(cwd), "reason": "exit"})


def wait_for(predicate: Any, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_session_end_returns_immediately_and_posts_in_background(sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    start = now() - dt.timedelta(minutes=30)
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=5)))
    transcript = transcript_at(sandbox, start)
    code = sandbox.home / "code"
    code.mkdir()

    began = time.monotonic()
    # gh is slow: if the hook did the work in the foreground, or kept its
    # stdout pipe open in the worker, this call could not return quickly.
    proc = run_session_end(sandbox, hook_json(transcript, code), FAKE_GH_SLEEP="1.5")
    elapsed = time.monotonic() - began

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert elapsed < 1.5, f"session-end blocked for {elapsed:.2f}s"
    assert wait_for(lambda: len(sandbox.posted()) == 1), sandbox.log_text()
    post = sandbox.posted()[0]
    assert post["url"] == FAKE_WEBHOOK + "?wait=true"
    assert post["body"]["allowed_mentions"] == {"parse": []}
    assert "#163" in json.dumps(post["body"])
    assert sandbox.op_argv() == [], "a fresh cache must not call op at all"
    assert FAKE_WEBHOOK not in sandbox.log_text()


@pytest.mark.parametrize("stdin", ["", "not json", "{}", '{"transcript_path": "/nonexistent/t.jsonl", "cwd": "/"}'])
def test_session_end_bad_input_exits_zero_silently(sandbox: Sandbox, stdin: str) -> None:
    proc = run_session_end(sandbox, stdin)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert sandbox.gh_calls.read_text() == ""


def test_session_end_unknown_argument_exits_zero_silently(sandbox: Sandbox) -> None:
    proc = run_session_end(sandbox, "{}", "--bogus")
    assert (proc.returncode, proc.stdout) == (0, "")


def test_session_end_unwritable_state_exits_zero_silently(sandbox: Sandbox) -> None:
    transcript = transcript_at(sandbox, now() - dt.timedelta(hours=1))
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home),
                           LAB_CHANGELOG_STATE_DIR="/proc/lab-changelog-cannot-exist")
    assert (proc.returncode, proc.stdout) == (0, "")


def test_session_end_worker_failure_is_logged_not_raised(sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    transcript = transcript_at(sandbox, now() - dt.timedelta(hours=1))
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home), FAKE_GH_FAIL="1")
    assert (proc.returncode, proc.stdout) == (0, "")
    assert wait_for(lambda: "every gh query failed" in sandbox.log_text()), sandbox.log_text()
    assert sandbox.posted() == []


def test_session_end_inside_its_own_claude_child_does_nothing(sandbox: Sandbox) -> None:
    transcript = transcript_at(sandbox, now() - dt.timedelta(hours=1))
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home), LAB_CHANGELOG_CHILD="1")
    assert (proc.returncode, proc.stdout) == (0, "")
    time.sleep(0.5)
    assert sandbox.gh_calls.read_text() == ""


def test_session_end_skips_herdr_worktree_sessions(sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=5)))
    transcript = transcript_at(sandbox, now() - dt.timedelta(hours=2))
    worktree = sandbox.home / ".herdr" / "worktrees" / "ansible-quasarlab" / "feat-x"
    worktree.mkdir(parents=True)
    proc = run_session_end(sandbox, hook_json(transcript, worktree))
    assert (proc.returncode, proc.stdout) == (0, "")
    time.sleep(1)
    assert sandbox.gh_calls.read_text() == ""
    assert sandbox.posted() == []
    assert "herdr worktree" in sandbox.log_text()


def test_session_end_skips_short_sessions(sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=1)))
    transcript = transcript_at(sandbox, now() - dt.timedelta(minutes=9))
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home))
    assert (proc.returncode, proc.stdout) == (0, "")
    time.sleep(1)
    assert sandbox.gh_calls.read_text() == ""
    assert sandbox.posted() == []
    assert "under the 600s minimum" in sandbox.log_text()


def test_session_skip_reason_rules(lc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    end = now()
    long_start = end - dt.timedelta(minutes=11)
    worktrees = tmp_path / ".herdr" / "worktrees"
    (worktrees / "r" / "b").mkdir(parents=True)
    lookalike = tmp_path / ".herdr" / "worktrees-old"
    lookalike.mkdir()
    link = tmp_path / "link-to-worktree"
    link.symlink_to(worktrees / "r" / "b")

    def reason(cwd: str, start: dt.datetime = long_start, min_secs: int = 600) -> str | None:
        return lc.session_skip_reason(lc.HookInput("s", "/t", cwd, "exit"), start, end, min_secs)

    assert reason(str(worktrees / "r" / "b")) is not None
    assert reason(str(link)) is not None, "a symlink into a worktree is still a worktree"
    assert reason(str(lookalike)) is None
    assert reason(str(tmp_path)) is None
    assert reason(str(tmp_path), start=end - dt.timedelta(minutes=9)) is not None
    assert reason(str(tmp_path), start=end - dt.timedelta(minutes=9), min_secs=60) is None


def test_session_end_dry_run_prints_payload_for_transcript_window(sandbox: Sandbox) -> None:
    start = now() - dt.timedelta(minutes=45)
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=5)))
    transcript = transcript_at(sandbox, start)
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    payloads = json.loads(proc.stdout)["payloads"]
    assert start.strftime("%Y-%m-%d %H:%M") in payloads[0]["embeds"][0]["title"]
    searches = [json.loads(line) for line in sandbox.gh_calls.read_text().splitlines()]
    assert all(args[args.index("--search") + 1].endswith(f">={iso(start)}") for args in searches)
    assert sandbox.posted() == []
    assert sandbox.op_argv() == []


# ---------------------------------------------------------------------------
# Collection, dedup, state
# ---------------------------------------------------------------------------


def test_collect_uses_verified_gh_queries(lc: Any) -> None:
    calls: list[list[str]] = []
    start = dt.datetime(2026, 9, 12, 20, tzinfo=dt.timezone.utc)
    lc.collect(["mithr4ndir/k8s-argocd"], start, now(), fake_runner({}, calls))
    searches = {(c[1], c[c.index("--state") + 1], c[c.index("--search") + 1]) for c in calls}
    assert searches == {
        ("pr", "merged", "merged:>=2026-09-12T20:00:00Z"),
        ("issue", "all", "created:>=2026-09-12T20:00:00Z"),
        ("issue", "closed", "closed:>=2026-09-12T20:00:00Z"),
    }
    assert all(c[0] == "gh" for c in calls)


def test_collect_filters_by_timestamp_and_rebuilds_urls(lc: Any) -> None:
    start = now() - dt.timedelta(hours=1)
    rows = {
        "pr:merged:mithr4ndir/ansible-quasarlab": [
            {"number": 1, "title": "in window", "url": "https://evil.example/phish", "mergedAt": iso(now() - dt.timedelta(minutes=5))},
            {"number": 2, "title": "too old", "url": "u", "mergedAt": iso(now() - dt.timedelta(hours=3))},
            {"number": "3", "title": "bad number", "mergedAt": iso(now())},
        ],
        "issue:all:mithr4ndir/ansible-quasarlab": [
            {"number": 4, "title": "a PR", "url": "https://github.com/mithr4ndir/ansible-quasarlab/pull/4", "createdAt": iso(now())},
        ],
    }
    items = lc.collect(["mithr4ndir/ansible-quasarlab"], start, now() + dt.timedelta(minutes=1), fake_runner(rows))
    assert [(it.kind, it.number) for it in items] == [("pr_merged", 1)]
    assert items[0].url == "https://github.com/mithr4ndir/ansible-quasarlab/pull/1"


def test_dedup_second_run_posts_nothing(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    cfg = config(lc, sandbox, claude_bin="")
    start, end = now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1)

    first = FakeOpener()
    assert lc.run_changelog(cfg, start, end, dry_run=False, use_llm=False, runner=fake_runner(rows), opener=first) == 1
    assert len(first.requests) == 1
    state = json.loads((sandbox.state / "posted.json").read_text())["posted"]
    assert set(state) == {
        "mithr4ndir/ansible-quasarlab#163:pr_merged",
        "mithr4ndir/ansible-quasarlab#164:issue_opened",
        "mithr4ndir/k8s-argocd#201:issue_closed",
    }

    second = FakeOpener()
    assert lc.run_changelog(cfg, start, end, dry_run=False, use_llm=False, runner=fake_runner(rows), opener=second) == 0
    assert second.requests == []


def test_nothing_new_fetches_no_secret(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any) -> None:
        raise AssertionError("no secret may be fetched when there is nothing to post")

    monkeypatch.setattr(lc, "resolve_webhook", forbidden)
    opener = FakeOpener()
    cfg = config(lc, sandbox)
    assert lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now(), dry_run=False, use_llm=False,
                            runner=fake_runner({}), opener=opener) == 0
    assert opener.requests == []


def test_only_new_items_are_posted(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    sandbox.state.mkdir()
    (sandbox.state / "posted.json").write_text(json.dumps({"version": 1, "posted": {
        "mithr4ndir/ansible-quasarlab#163:pr_merged": iso(now()),
    }}))
    opener = FakeOpener()
    cfg = config(lc, sandbox)
    lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                     use_llm=False, runner=fake_runner(rows), opener=opener)
    body = json.dumps(opener.bodies())
    assert "/pull/163" not in body
    assert "/issues/164" in body and "/issues/201" in body


def test_opened_and_closed_same_issue_are_separate_keys(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    stamp = iso(now() - dt.timedelta(minutes=5))
    row = {"number": 9, "title": "quick fix", "url": "", "body": "", "createdAt": stamp, "closedAt": stamp}
    rows = {"issue:all:mithr4ndir/ansible-quasarlab": [row], "issue:closed:mithr4ndir/ansible-quasarlab": [row]}
    opener = FakeOpener()
    cfg = config(lc, sandbox)
    lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                     use_llm=False, runner=fake_runner(rows), opener=opener)
    state = json.loads((sandbox.state / "posted.json").read_text())["posted"]
    assert set(state) == {"mithr4ndir/ansible-quasarlab#9:issue_opened", "mithr4ndir/ansible-quasarlab#9:issue_closed"}


def test_failed_post_records_nothing(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    opener = FakeOpener([urllib.error.HTTPError("x", 400, "Bad Request", {}, BytesIO(b"{}"))])
    cfg = config(lc, sandbox)
    with pytest.raises(lc.PostError):
        lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                         use_llm=False, runner=fake_runner(rows), opener=opener, sleep=lambda s: None)
    assert not (sandbox.state / "posted.json").exists()


def test_partial_split_post_records_only_sent_parts(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    stamp = iso(now() - dt.timedelta(minutes=5))
    rows = {"pr:merged:mithr4ndir/ansible-quasarlab": [
        {"number": n, "title": "x" * 140, "url": "", "body": "", "mergedAt": stamp} for n in range(1, 101)
    ]}
    opener = FakeOpener()
    calls = {"n": 0}

    def flaky(request: Any, timeout: float | None = None) -> FakeResponse:
        calls["n"] += 1
        if calls["n"] == 2:
            raise urllib.error.HTTPError("x", 403, "Forbidden", {}, BytesIO(b"{}"))
        return opener(request, timeout)

    cfg = config(lc, sandbox)
    with pytest.raises(lc.PostError):
        lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                         use_llm=False, runner=fake_runner(rows), opener=flaky, sleep=lambda s: None)
    state = json.loads((sandbox.state / "posted.json").read_text())["posted"]
    sent_numbers = {int(m) for m in re.findall(r"/pull/(\d+)\)", json.dumps(opener.bodies()))}
    assert sent_numbers and len(sent_numbers) < 100
    assert {int(k.split("#")[1].split(":")[0]) for k in state} == sent_numbers


def test_state_is_written_atomically_and_pruned(lc: Any, tmp_path: Path) -> None:
    old = iso(now() - dt.timedelta(days=200))
    recent = iso(now())
    lc.save_state(tmp_path, {"a#1:pr_merged": old, "b#2:pr_merged": recent}, now())
    data = json.loads((tmp_path / "posted.json").read_text())
    assert data["posted"] == {"b#2:pr_merged": recent}
    assert [p.name for p in tmp_path.iterdir()] == ["posted.json"], "no temp files left behind"


def test_corrupt_state_is_treated_as_empty(lc: Any, tmp_path: Path) -> None:
    (tmp_path / "posted.json").write_text("{not json")
    assert lc.load_state(tmp_path) == {}


def test_lock_blocks_a_concurrent_run(lc: Any, tmp_path: Path) -> None:
    with lc.StateLock(tmp_path):
        with pytest.raises(lc.ChangelogError):
            with lc.StateLock(tmp_path, wait=0.2):
                pass
    with lc.StateLock(tmp_path, wait=0.2):
        pass


def test_lock_is_held_across_another_process(lc: Any, sandbox: Sandbox) -> None:
    sandbox.state.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
         "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(5)",
         str(sandbox.state / "lock")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(lc.ChangelogError):
            with lc.StateLock(sandbox.state, wait=0.3):
                pass
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------------------
# Untrusted titles
# ---------------------------------------------------------------------------


def unescaped(text: str, chars: str) -> list[str]:
    """Characters from `chars` that are not backslash escaped, Discord style."""
    found = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char in chars:
            found.append(char)
        index += 1
    return found


HOSTILE_TITLES = [
    "@everyone [fake](http://evil.example/login)",
    "\\[fake\\](http://evil.example) backslashes first",
    "\\\\[fake\\\\](http://evil.example) doubled backslashes",
    "<@&123456789> role ping and <@!42> user and @here",
    "[x]\u200b(http://evil.example) zero width split",
    "`code` **bold** __under__ ~~strike~~ ||spoiler|| > quote # head",
    "<https://evil.example> angle link and https://evil.example/raw",
    "control\x00\x1b[31mchars\u202eRTL\ttab\nnewline",
]


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_hostile_title_is_neutralized(lc: Any, title: str) -> None:
    safe = lc.discord_text(title)
    assert unescaped(safe, "[]()<>*_~`|#@") == [], safe
    assert "://" not in safe, "URL schemes are defanged so nothing auto-links"
    assert "@everyone" not in safe and "@here" not in safe
    assert not any(ord(ch) < 0x20 or ch in "\u202e\x7f" for ch in safe)


def test_hostile_title_cannot_forge_a_link_in_the_payload(lc: Any) -> None:
    item = make_item(lc, number=7, title="@everyone [fake](http://evil.example)")
    payload = lc.build_messages([item], now(), now(), "command-center1", [item.title], False)[0][0]
    embed = payload["embeds"][0]
    value = embed["fields"][0]["value"]
    links = re.findall(r"(?<!\\)\[(.*?)(?<!\\)\]\((.*?)\)", value)
    assert [target for _, target in links] == ["https://github.com/mithr4ndir/ansible-quasarlab/pull/7"]
    assert "evil.example" not in json.dumps([target for _, target in links])
    assert "@everyone" not in json.dumps(payload, ensure_ascii=False)
    assert payload["allowed_mentions"] == {"parse": []}


def test_bracket_first_then_general_escape_would_reopen_links(lc: Any) -> None:
    """Why discord_text escapes in one pass.

    Escaping [ and ] first and then running a general escaper that also
    escapes backslashes turns an input of \\[ into \\\\\\[, where the
    backslash is escaped and the bracket is live again.
    """

    def naive(text: str) -> str:
        text = text.replace("[", "\\[").replace("]", "\\]")
        return re.sub(r"([\\*_~`|>()])", r"\\\1", text)

    hostile = "[fake](http://evil.example)"
    assert unescaped(naive(hostile), "[]") != [], "the naive order is exploitable"
    assert unescaped(lc.discord_text(hostile), "[]") == []


def test_title_length_is_capped(lc: Any) -> None:
    safe = lc.discord_text("a" * 5000)
    assert len(safe) <= lc.TITLE_MAX_CHARS
    assert safe.endswith("…")


# ---------------------------------------------------------------------------
# Payload limits
# ---------------------------------------------------------------------------


def assert_within_discord_limits(lc: Any, payload: dict[str, Any]) -> None:
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert len(embed["title"]) <= 256
    assert len(embed.get("description", "")) <= 4096
    assert len(embed.get("fields", [])) <= 25
    for fld in embed.get("fields", []):
        assert 0 < len(fld["value"]) <= 1024
        assert 0 < len(fld["name"]) <= 256
    assert len(embed["footer"]["text"]) <= 2048
    assert lc.embed_size(embed) <= 6000


def test_large_changelog_is_split_within_limits(lc: Any) -> None:
    repos = [f"mithr4ndir/repo-{n}" for n in range(12)]
    items = [
        make_item(lc, kind=kind, number=n, repo=repo, title="[x](y) " + "word_" * 40)
        for repo in repos for kind in lc.KINDS for n in range(1, 9)
    ]
    highlights = ["h" * 1000] * 5
    messages = lc.build_messages(items, now(), now(), "command-center1", highlights, True)
    assert len(messages) > 1
    for index, (payload, _) in enumerate(messages, start=1):
        assert_within_discord_limits(lc, payload)
        assert f"(part {index} of {len(messages)})" in payload["embeds"][0]["title"]
    all_keys = [key for _, keys in messages for key in keys]
    assert sorted(all_keys) == sorted(item.key for item in items), "every item exactly once"
    body = json.dumps([payload for payload, _ in messages])
    for item in items:
        # An issue opened and closed in the window is listed under both.
        assert body.count(f"({item.url})") == sum(1 for other in items if other.url == item.url)


def test_many_small_fields_split_on_the_25_field_limit(lc: Any) -> None:
    items = [make_item(lc, number=1, repo=f"mithr4ndir/r{n:02d}") for n in range(60)]
    messages = lc.build_messages(items, now(), now(), "command-center1", [], False)
    assert [len(p["embeds"][0]["fields"]) for p, _ in messages] == [25, 25, 10]
    for payload, _ in messages:
        assert_within_discord_limits(lc, payload)


def test_single_message_has_counts_highlights_and_theme(lc: Any) -> None:
    items = [make_item(lc, "pr_merged", 1), make_item(lc, "issue_opened", 2), make_item(lc, "issue_closed", 3)]
    start = dt.datetime(2026, 9, 12, 20, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 13, 20, tzinfo=dt.timezone.utc)
    (payload, keys), = lc.build_messages(items, start, end, "command-center1", ["did a thing"], True)
    embed = payload["embeds"][0]
    assert embed["title"] == "🧙 command-center1 changelog: 2026-09-12 20:00 to 2026-09-13 20:00 UTC"
    assert "1 PR merged" in embed["description"] and "1 issue opened" in embed["description"]
    assert "Highlights (generated summary)" in embed["description"]
    assert [f["name"] for f in embed["fields"]] == [
        "🪄 PRs merged: ansible-quasarlab", "📜 Issues opened: ansible-quasarlab", "💍 Issues closed: ansible-quasarlab"]
    assert len(keys) == 3
    assert "\u2014" not in json.dumps(payload, ensure_ascii=False)


def test_every_posted_payload_has_allowed_mentions(lc: Any, sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    stamp = iso(now() - dt.timedelta(minutes=5))
    rows = {"pr:merged:mithr4ndir/ansible-quasarlab": [
        {"number": n, "title": "@everyone " + "y" * 140, "url": "", "body": "", "mergedAt": stamp} for n in range(1, 120)
    ]}
    opener = FakeOpener()
    cfg = config(lc, sandbox)
    sent = lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                            use_llm=False, runner=fake_runner(rows), opener=opener)
    assert sent == len(opener.requests) > 1
    for body in opener.bodies():
        assert body["allowed_mentions"] == {"parse": []}


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def http_error(code: int, body: bytes = b"{}", headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(FAKE_WEBHOOK, code, "err", headers or {}, BytesIO(body))


def test_post_retries_429_honoring_retry_after(lc: Any) -> None:
    sleeps: list[float] = []
    opener = FakeOpener([http_error(429, b'{"retry_after": 2.5}'), http_error(429, b'{"retry_after": 0.4}')])
    lc.post_payload(FAKE_WEBHOOK, {"allowed_mentions": {"parse": []}}, opener=opener, sleep=sleeps.append)
    assert sleeps == [2.5, 0.4]
    assert len(opener.requests) == 3
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"


def test_post_backs_off_on_5xx_and_gives_up_on_4xx(lc: Any) -> None:
    sleeps: list[float] = []
    opener = FakeOpener([http_error(502), http_error(503)])
    lc.post_payload(FAKE_WEBHOOK, {}, opener=opener, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]

    opener = FakeOpener([http_error(401)])
    with pytest.raises(lc.PostError):
        lc.post_payload(FAKE_WEBHOOK, {}, opener=opener, sleep=sleeps.append)
    assert len(opener.requests) == 1


def test_post_gives_up_after_repeated_429(lc: Any) -> None:
    opener = FakeOpener([http_error(429, b'{"retry_after": 999}')] * 10)
    sleeps: list[float] = []
    with pytest.raises(lc.PostError):
        lc.post_payload(FAKE_WEBHOOK, {}, opener=opener, sleep=sleeps.append)
    assert len(opener.requests) == lc.POST_MAX_ATTEMPTS
    assert max(sleeps) <= lc.POST_MAX_SLEEP_SECS


@pytest.mark.parametrize("failure", [
    urllib.error.URLError(f"cannot reach {FAKE_WEBHOOK}"),
    ValueError(f"unknown url type: {FAKE_WEBHOOK}"),
    OSError(f"connection reset talking to {FAKE_WEBHOOK}"),
    http_error(500),
    http_error(404),
])
def test_post_errors_never_carry_the_webhook(lc: Any, failure: BaseException) -> None:
    opener = FakeOpener([failure] * 10)
    with pytest.raises(lc.PostError) as caught:
        lc.post_payload(FAKE_WEBHOOK, {}, opener=opener, sleep=lambda s: None)
    exc = caught.value
    assert FAKE_WEBHOOK not in str(exc) and "webhooks" not in repr(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    import traceback
    assert "webhooks" not in "".join(traceback.format_exception(exc))


def test_webhook_never_logged_on_unexpected_failure(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    sandbox.seed_webhook()
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    monkeypatch.setattr(lc, "run_gh", fake_runner(rows))

    def exploding(request: Any, timeout: float | None = None) -> Any:
        # Not a type post_payload handles: it escapes to main's last resort
        # handler and its traceback, URL included, is logged.
        raise RuntimeError(f"boom while posting to {request.full_url}")

    monkeypatch.setattr(lc.urllib.request, "urlopen", exploding)
    assert lc.main(["daily", "--no-llm"]) == 1
    captured = capsys.readouterr()
    log_text = sandbox.log_text()
    assert "RuntimeError" in log_text, "the failure itself is still logged"
    for text in (log_text, captured.out, captured.err):
        assert FAKE_WEBHOOK not in text
        assert "FAKE-token_value" not in text


def test_redaction_covers_unregistered_webhook_urls(lc: Any) -> None:
    other = "https://discordapp.com/api/webhooks/1/abc-DEF"
    assert "abc-DEF" not in lc.redact(f"failed: {other}?wait=true")


# ---------------------------------------------------------------------------
# Webhook secret through the real cache libraries
# ---------------------------------------------------------------------------


def test_webhook_served_from_fresh_cache_without_op(lc: Any, sandbox: Sandbox) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    sandbox.seed_webhook()
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(sandbox.root / "no-token")) == FAKE_WEBHOOK
    assert sandbox.op_argv() == []


def test_active_killswitch_and_no_cache_means_no_post(lc: Any, sandbox: Sandbox) -> None:
    sandbox.trip_killswitch()
    token = sandbox.root / "token"
    token.write_text("fake-token")
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    opener = FakeOpener()
    cfg = config(lc, sandbox, token_file=str(token))
    sent = lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                            use_llm=False, runner=fake_runner(rows), opener=opener)
    assert sent == 0
    assert opener.requests == []
    assert not any(line.startswith("read") for line in sandbox.op_argv())
    assert "not posting" in sandbox.log_text()
    assert not (sandbox.state / "posted.json").exists(), "unposted items stay eligible"


def test_missing_webhook_means_no_post_and_exit_zero(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    monkeypatch.setattr(lc, "run_gh", fake_runner(rows))
    opener = FakeOpener()
    monkeypatch.setattr(lc.urllib.request, "urlopen", opener)
    # No cache, no token: cached_op_read cannot produce a value.
    assert lc.main(["daily", "--no-llm"]) == 0
    assert opener.requests == []
    assert not any(line.startswith("read") for line in sandbox.op_argv())
    assert "no value from the 1Password cache" in sandbox.log_text()


def test_missing_op_libraries_means_no_post(lc: Any, sandbox: Sandbox, tmp_path: Path) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    assert lc.resolve_webhook(str(tmp_path / "nope"), lc.DEFAULT_OP_REFERENCE, "") is None
    assert "op-killswitch.sh missing" in sandbox.log_text()
    assert sandbox.op_argv() == []


def test_spent_quota_never_refreshes_the_cache(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.setenv("FAKE_OP_REMAINING", "10")
    # A Claude session may already export the token. The guard must drop it,
    # not merely decline to load it from the file.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "fake-token-in-env")
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(token)) is None
    assert "service-account ratelimit" in sandbox.op_argv()
    assert not any(line.startswith("read") for line in sandbox.op_argv())


def test_spent_quota_still_serves_a_stale_cache(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    sandbox.seed_webhook(age_secs=10 * 86400)
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.setenv("FAKE_OP_REMAINING", "10")
    # A Claude session may already export the token. The guard must drop it,
    # not merely decline to load it from the file.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "fake-token-in-env")
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(token)) == FAKE_WEBHOOK
    assert not any(line.startswith("read") for line in sandbox.op_argv())


def test_stale_cache_with_quota_refreshes_through_the_cache_library(lc: Any, sandbox: Sandbox,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    """The one path that may read 1Password: the cache library's own refresh.

    The fake op returns a non-webhook value, which is rejected, so this also
    proves a refreshed value is validated before use.
    """
    sandbox.seed_webhook(age_secs=10 * 86400)
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.setenv("FAKE_OP_REMAINING", "900")
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(token)) is None
    reads = [line for line in sandbox.op_argv() if line.startswith("read")]
    assert reads == [f"read {lc.DEFAULT_OP_REFERENCE}"]
    assert "not a Discord webhook URL" in sandbox.log_text()
    assert "value-from-fake-op-read" not in sandbox.log_text()


@pytest.mark.parametrize("value", ["", "https://evil.example/api/webhooks/1/x", "not a url"])
def test_cached_value_must_be_a_discord_webhook(lc: Any, sandbox: Sandbox, value: str) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    if value:
        sandbox.seed_webhook(value)
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, "") is None
    if value:
        assert value not in sandbox.log_text()


def test_malformed_reference_is_refused_before_bash(lc: Any, sandbox: Sandbox) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    assert lc.resolve_webhook(str(LIB_DIR), 'op://x/y"; rm -rf ~ #', "") is None
    assert sandbox.op_argv() == []


def test_dry_run_fetches_no_secret_and_posts_nothing(sandbox: Sandbox) -> None:
    sandbox.seed_webhook()
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=5)))
    proc = subprocess.run([sys.executable, str(SCRIPT), "daily", "--dry-run"], env=sandbox.env(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    payloads = json.loads(proc.stdout)["payloads"]
    assert len(payloads) == 1 and payloads[0]["allowed_mentions"] == {"parse": []}
    assert sandbox.posted() == []
    assert sandbox.op_argv() == []
    assert not (sandbox.state / "posted.json").exists()
    assert FAKE_WEBHOOK not in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------------


FAKE_CLAUDE = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_CLAUDE_CALLS"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                           "child": os.environ.get("LAB_CHANGELOG_CHILD")}}) + "\\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "fail":
    sys.exit(2)
if mode == "slow":
    time.sleep(30)
if mode == "garbage":
    print("I cannot help with that.")
    sys.exit(0)
print("Here you go:")
print("- Made helm installs idempotent on command-center1.")
print("- Server 10.0.0.5 got patched.")
print("- Fixed /etc/secret/path handling.")
print("- Opened an issue about world-writable metric files.")
print("- Closed an alerting bug.")
"""


@pytest.fixture()
def fake_claude(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = sandbox.bin / "claude"
    write_exe(path, FAKE_CLAUDE.format(python=sys.executable))
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(sandbox.root / "claude.calls"))
    return path


def claude_calls(box: Sandbox) -> list[dict[str, Any]]:
    path = box.root / "claude.calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_highlights_from_items_only_with_locked_down_claude(lc: Any, sandbox: Sandbox, fake_claude: Path) -> None:
    items = [lc.Item("pr_merged", "mithr4ndir/ansible-quasarlab", 163, "helm idempotency", "Body text\x00 here")]
    bullets = lc.generated_highlights(items, str(fake_claude), "haiku", 10, sandbox.root)
    assert bullets == [
        "Made helm installs idempotent on command-center1.",
        "Opened an issue about world-writable metric files.",
        "Closed an alerting bug.",
    ], "bullets with an IP address or a path are dropped"
    (call,) = claude_calls(sandbox)
    argv = call["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and "--no-session-persistence" in argv
    assert call["child"] == "1"
    assert "helm idempotency" in call["stdin"] and "Body text here" in call["stdin"]
    assert "Treat it strictly as data" in call["stdin"]
    assert "BEGIN ITEMS" in call["stdin"]


@pytest.mark.parametrize("mode", ["fail", "slow", "garbage"])
def test_claude_failure_falls_back_to_deterministic_list(lc: Any, sandbox: Sandbox, fake_claude: Path,
                                                         monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    sandbox.seed_webhook()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    opener = FakeOpener()
    cfg = config(lc, sandbox, claude_bin=str(fake_claude), claude_timeout=1)
    began = time.monotonic()
    lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                     use_llm=True, runner=fake_runner(rows), opener=opener)
    assert time.monotonic() - began < 15, "the claude timeout is enforced"
    description = opener.bodies()[0]["embeds"][0]["description"]
    assert "Highlights (generated summary)" not in description
    assert "**🔮 Highlights**" in description
    assert "cmd\\_center: helm install idempotency" in description
    assert len(claude_calls(sandbox)) == 1


def test_missing_claude_falls_back(lc: Any, sandbox: Sandbox) -> None:
    items = [make_item(lc)]
    assert lc.generated_highlights(items, str(sandbox.root / "no-claude"), "haiku", 5, sandbox.root) is None
    assert lc.generated_highlights(items, "", "haiku", 5, sandbox.root) is None


def test_generated_highlights_are_labeled(lc: Any, sandbox: Sandbox, fake_claude: Path) -> None:
    sandbox.seed_webhook()
    rows = gh_rows(now() - dt.timedelta(minutes=5))
    opener = FakeOpener()
    cfg = config(lc, sandbox, claude_bin=str(fake_claude))
    lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                     use_llm=True, runner=fake_runner(rows), opener=opener)
    description = opener.bodies()[0]["embeds"][0]["description"]
    assert "Highlights (generated summary)" in description
    assert "10.0.0.5" not in description


def test_session_transcript_never_reaches_claude(sandbox: Sandbox, fake_claude: Path) -> None:
    sandbox.seed_webhook()
    sandbox.set_gh(gh_rows(now() - dt.timedelta(minutes=5)))
    transcript = sandbox.root / "transcript.jsonl"
    transcript.write_text(json.dumps({"timestamp": iso(now() - dt.timedelta(hours=1)),
                                      "message": "TRANSCRIPT-CANARY op://Infrastructure/secret"}) + "\n")
    proc = run_session_end(sandbox, hook_json(transcript, sandbox.home),
                           LAB_CHANGELOG_CLAUDE_BIN=str(fake_claude),
                           FAKE_CLAUDE_CALLS=str(sandbox.root / "claude.calls"))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert wait_for(lambda: len(sandbox.posted()) == 1), sandbox.log_text()
    calls = claude_calls(sandbox)
    assert len(calls) == 1
    assert "TRANSCRIPT-CANARY" not in json.dumps(calls)
    assert "TRANSCRIPT-CANARY" not in json.dumps(sandbox.posted()) + sandbox.log_text()


# ---------------------------------------------------------------------------
# Configuration file and role wiring
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def render(template: str, variables: dict[str, Any]) -> str:
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    # Ansible's bool filter, for the values the templates use.
    env.filters["bool"] = lambda value: str(value).strip().lower() in ("true", "yes", "on", "1")
    return env.from_string(template).render(**variables)


def role_vars() -> dict[str, Any]:
    base = {
        "ansible_user": "ladino",
        "inventory_hostname": "command-center1",
        "ansible_managed": "Ansible managed",
    }
    defaults = load_yaml(DEFAULTS)
    merged = {**base, **defaults}
    # Resolve the few defaults that reference other variables.
    for _ in range(3):
        merged = {k: render(v, merged) if isinstance(v, str) and "{{" in v else v for k, v in merged.items()}
    return merged


def test_role_defaults_match_script_defaults(lc: Any) -> None:
    defaults = load_yaml(DEFAULTS)
    assert defaults["cmd_center_changelog_enabled"] is False
    assert tuple(defaults["cmd_center_changelog_repos"]) == lc.DEFAULT_REPOS
    assert defaults["cmd_center_changelog_op_reference"] == lc.DEFAULT_OP_REFERENCE
    assert defaults["cmd_center_changelog_session_min_secs"] == lc.DEFAULT_SESSION_MIN_SECS == 600
    assert load_yaml(HOST_VARS)["cmd_center_changelog_enabled"] is True


def test_main_imports_changelog_gated_on_flag() -> None:
    tasks = load_yaml(MAIN)
    (entry,) = [t for t in tasks if t.get("ansible.builtin.import_tasks") == "changelog.yml"]
    assert entry["when"] == "cmd_center_changelog_enabled | bool"


def test_changelog_tasks_install_script_units_and_timer() -> None:
    tasks = {t["name"]: t for t in load_yaml(TASKS)}
    for task in tasks.values():
        assert task.get("tags") == ["changelog"]
    copy = next(t["ansible.builtin.copy"] for t in tasks.values() if "ansible.builtin.copy" in t)
    assert copy == {"src": "lab-changelog", "dest": "/usr/local/bin/lab-changelog",
                    "owner": "root", "group": "root", "mode": "0755"}
    assert (ROLE / "files" / copy["src"]).is_file()
    templates = [t["ansible.builtin.template"] for t in tasks.values() if "ansible.builtin.template" in t]
    assert {t["dest"] for t in templates} == {
        "/etc/default/lab-changelog", "/etc/systemd/system/lab-changelog.service", "/etc/systemd/system/lab-changelog.timer"}
    for tmpl in templates:
        assert (TEMPLATES / tmpl["src"]).is_file()
        assert (tmpl["owner"], tmpl["mode"]) == ("root", "0644")
    enable = next(t["ansible.builtin.systemd"] for t in tasks.values() if "ansible.builtin.systemd" in t)
    assert enable["name"] == "lab-changelog.timer" and enable["enabled"] is True
    assert "scope" not in enable, "a system timer, not a user one"
    handlers = {h["name"] for h in load_yaml(HANDLERS)}
    for task in tasks.values():
        for handler in task.get("notify", []):
            assert handler in handlers


def test_rendered_units() -> None:
    variables = role_vars()
    service = render((TEMPLATES / "lab-changelog.service.j2").read_text(), variables)
    timer = render((TEMPLATES / "lab-changelog.timer.j2").read_text(), variables)
    assert "Type=oneshot" in service
    assert "User=ladino" in service
    assert "ExecStart=/usr/local/bin/lab-changelog daily" in service
    assert "Environment=HOME=/home/ladino" in service
    assert re.search(r"^TimeoutStartSec=\d+$", service, re.M)
    assert "OnCalendar=*-*-* 23:55:00 UTC" in timer
    assert "Persistent=true" in timer
    calendar = subprocess.run(["systemd-analyze", "calendar", "*-*-* 23:55:00 UTC"],
                              capture_output=True, text=True, check=True)
    assert "23:55:00 UTC" in calendar.stdout
    assert "\u2014" not in service + timer


def test_rendered_settings_file_configures_the_script(lc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = render((TEMPLATES / "lab-changelog.default.j2").read_text(), role_vars())
    path = tmp_path / "lab-changelog"
    path.write_text(rendered + "PATH=/evil\nOP_SERVICE_ACCOUNT_TOKEN=nope\n")
    for key in list(os.environ):
        if key.startswith("LAB_CHANGELOG_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LAB_CHANGELOG_CONFIG", str(path))
    cfg = lc.Config.from_env()
    assert tuple(cfg.repos) == lc.DEFAULT_REPOS
    assert cfg.reference == lc.DEFAULT_OP_REFERENCE
    assert cfg.lib_dir == "/var/lib/ansible-quasarlab/repo/scripts/lib"
    assert cfg.token_file == "/home/ladino/.config/op/service-account-token"
    assert cfg.session_min_secs == 600
    assert cfg.claude_bin == "/home/ladino/.local/bin/claude"
    assert cfg.host == "command-center1"
    assert lc.load_config_file(str(path)).keys() <= lc.CONFIG_KEYS
    monkeypatch.setenv("LAB_CHANGELOG_SESSION_MIN_SECS", "60")
    assert lc.Config.from_env().session_min_secs == 60, "environment wins over the file"


def test_no_shell_true_and_no_em_dash() -> None:
    source = SCRIPT.read_text()
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "\u2014" not in source
    for path in (TASKS, TEMPLATES / "lab-changelog.service.j2", TEMPLATES / "lab-changelog.timer.j2",
                 TEMPLATES / "lab-changelog.default.j2", Path(__file__)):
        assert "\u2014" not in path.read_text()


def test_window_scan_is_bounded(lc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "TRANSCRIPT_SCAN_BYTES", 1024)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": "x" * 4096}) + "\n" + json.dumps({"timestamp": "2026-09-13T10:00:00Z"}) + "\n")
    with pytest.raises(lc.ChangelogError):
        lc.transcript_start(str(transcript))


# ---------------------------------------------------------------------------
# Review fix 1: the daily window starts at the last successful daily run
# ---------------------------------------------------------------------------

T0 = dt.datetime(2026, 9, 10, 23, 55, tzinfo=dt.timezone.utc)
ANSIBLE = "mithr4ndir/ansible-quasarlab"
ARGOCD = "mithr4ndir/k8s-argocd"


def pr_row(number: int, when: dt.datetime) -> dict[str, Any]:
    return {"number": number, "title": f"change {number}", "url": "", "body": "", "mergedAt": iso(when)}


class DailyHarness:
    """Drives `lab-changelog daily` through main() at a chosen wall clock.

    The webhook lookup is stubbed out here so these tests exercise only the
    window logic; the cache itself is covered by the review fix 2 tests.
    """

    def __init__(self, lc: Any, box: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lc = lc
        self.box = box
        self.monkeypatch = monkeypatch
        self.posted_numbers: list[int] = []
        monkeypatch.setattr(lc, "resolve_webhook", lambda *args, **kwargs: FAKE_WEBHOOK)

    def run(self, at: dt.datetime, prs: dict[str, list[dict[str, Any]]], *extra: str,
            fail_repos: tuple[str, ...] = (), post_status: int | None = None) -> tuple[int, list[int]]:
        lc = self.lc
        rows = {f"pr:merged:{repo}": value for repo, value in prs.items()}
        good = fake_runner(rows)

        def runner(argv: list[str], timeout: int) -> str:
            if argv[argv.index("--repo") + 1] in fail_repos:
                raise lc.ChangelogError("gh exited 1: simulated outage")
            return good(argv, timeout)

        failures = [http_error(post_status)] * 10 if post_status else []
        opener = FakeOpener(failures)
        self.monkeypatch.setattr(lc, "utcnow", lambda: at)
        self.monkeypatch.setattr(lc, "run_gh", runner)
        self.monkeypatch.setattr(lc.urllib.request, "urlopen", opener)
        code = lc.main(["daily", "--no-llm", *extra])
        numbers = sorted(int(n) for n in re.findall(r"/pull/(\d+)\)", json.dumps(opener.bodies()))) \
            if not failures else []
        return code, numbers


@pytest.fixture()
def daily(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> DailyHarness:
    return DailyHarness(lc, sandbox, monkeypatch)


def test_first_daily_run_looks_back_24_hours(daily: DailyHarness) -> None:
    prs = {ANSIBLE: [pr_row(1, T0 - dt.timedelta(hours=23)), pr_row(2, T0 - dt.timedelta(hours=25))]}
    assert daily.run(T0, prs) == (0, [1])


def test_daily_runs_25_hours_apart_lose_nothing(daily: DailyHarness) -> None:
    prs = {ANSIBLE: [pr_row(1, T0 - dt.timedelta(hours=2))]}
    assert daily.run(T0, prs) == (0, [1])
    # Merged half an hour after the first run. A fixed 24 hour lookback from a
    # run 25 hours later starts at T0+1h and never sees it.
    prs[ANSIBLE].append(pr_row(2, T0 + dt.timedelta(minutes=30)))
    assert daily.run(T0 + dt.timedelta(hours=25), prs) == (0, [2])


def test_daily_host_down_for_three_days_loses_nothing(daily: DailyHarness) -> None:
    assert daily.run(T0, {}) == (0, [])
    prs = {ANSIBLE: [pr_row(n, T0 + dt.timedelta(hours=10 * n)) for n in range(1, 7)]}
    assert daily.run(T0 + dt.timedelta(days=3), prs) == (0, [1, 2, 3, 4, 5, 6])


def test_failed_post_does_not_advance_the_boundary(daily: DailyHarness) -> None:
    assert daily.run(T0 - dt.timedelta(hours=24), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(7, T0 - dt.timedelta(hours=3))]}
    code, _ = daily.run(T0, prs, post_status=400)
    assert code == 1
    # The next run is 25 hours after the failed one. Its window must reach
    # back to the last SUCCESSFUL run, so #7 is still posted.
    assert daily.run(T0 + dt.timedelta(hours=25), prs) == (0, [7])


def test_missing_webhook_does_not_advance_the_boundary(daily: DailyHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    assert daily.run(T0 - dt.timedelta(hours=24), {}) == (0, [])
    # Older than the overlap, so only a boundary that did not move keeps it.
    prs = {ANSIBLE: [pr_row(8, T0 - dt.timedelta(hours=3))]}
    monkeypatch.setattr(daily.lc, "resolve_webhook", lambda *args, **kwargs: None)
    assert daily.run(T0, prs) == (0, [])
    monkeypatch.setattr(daily.lc, "resolve_webhook", lambda *args, **kwargs: FAKE_WEBHOOK)
    assert daily.run(T0 + dt.timedelta(hours=25), prs) == (0, [8])


def test_github_failure_does_not_advance_the_boundary(daily: DailyHarness) -> None:
    assert daily.run(T0 - dt.timedelta(hours=24), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(1, T0 - dt.timedelta(hours=2))], ARGOCD: [pr_row(50, T0 - dt.timedelta(hours=3))]}
    # argocd is unreachable: what was collected is still posted...
    assert daily.run(T0, prs, fail_repos=(ARGOCD,)) == (0, [1])
    # ...but the boundary stays put, so argocd's item is not lost.
    assert daily.run(T0 + dt.timedelta(hours=25), prs) == (0, [50])


def test_overlap_catches_late_indexed_items_without_duplicates(daily: DailyHarness) -> None:
    posted_before = pr_row(1, T0 - dt.timedelta(minutes=10))
    assert daily.run(T0, {ANSIBLE: [posted_before]}) == (0, [1])
    # #2 merged five minutes before the first run but GitHub search had not
    # indexed it yet. The overlap re-reads that stretch; dedup keeps #1 out.
    late = pr_row(2, T0 - dt.timedelta(minutes=5))
    assert daily.run(T0 + dt.timedelta(hours=24), {ANSIBLE: [posted_before, late]}) == (0, [2])
    assert daily.run(T0 + dt.timedelta(hours=48), {ANSIBLE: [posted_before, late]}) == (0, [])


def test_lookback_is_capped_and_logged(daily: DailyHarness, lc: Any) -> None:
    assert daily.run(T0 - dt.timedelta(days=20), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(1, T0 - dt.timedelta(days=8)), pr_row(2, T0 - dt.timedelta(days=6))]}
    assert daily.run(T0, prs) == (0, [2])
    log_text = daily.box.log_text()
    assert "capped" in log_text and "7 days" in log_text


def test_since_overrides_the_boundary(daily: DailyHarness) -> None:
    assert daily.run(T0 - dt.timedelta(hours=2), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(3, T0 - dt.timedelta(days=4))]}
    assert daily.run(T0, prs, "--since", iso(T0 - dt.timedelta(days=5))) == (0, [3])


def test_narrow_since_run_does_not_advance_the_boundary(daily: DailyHarness) -> None:
    assert daily.run(T0 - dt.timedelta(hours=48), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(5, T0 - dt.timedelta(hours=30)), pr_row(6, T0 - dt.timedelta(hours=1))]}
    # A manual run over the last two hours only covers part of the gap.
    assert daily.run(T0, prs, "--since", iso(T0 - dt.timedelta(hours=2))) == (0, [6])
    assert daily.run(T0 + dt.timedelta(hours=1), prs) == (0, [5])


def test_dry_run_does_not_advance_the_boundary(daily: DailyHarness, capsys: pytest.CaptureFixture[str]) -> None:
    assert daily.run(T0 - dt.timedelta(hours=24), {}) == (0, [])
    prs = {ANSIBLE: [pr_row(4, T0 - dt.timedelta(minutes=30))]}
    assert daily.run(T0, prs, "--dry-run") == (0, [])
    assert "/pull/4" in capsys.readouterr().out
    assert daily.run(T0 + dt.timedelta(hours=25), prs) == (0, [4])


# ---------------------------------------------------------------------------
# Review fix 2: the webhook cache slug is keyed by the 1Password reference
# ---------------------------------------------------------------------------

REF_A = "op://Infrastructure/rmmf24ed3vvjffafar6wtah4ky/webhook_url"
REF_B = "op://Infrastructure/changelog-channel/webhook_url"
WEBHOOK_A = "https://discord.com/api/webhooks/111111111111111111/channel-A-token"
WEBHOOK_B = "https://discord.com/api/webhooks/222222222222222222/channel-B-token"


def expected_slug(reference: str) -> str:
    import hashlib
    return "discord_changelog_webhook_url." + hashlib.sha256(reference.encode()).hexdigest()[:16]


def cache_slugs(box: Sandbox) -> list[str]:
    return sorted(p.name for p in box.cache.iterdir() if not p.name.startswith("."))


@pytest.fixture()
def live_op(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.setenv("FAKE_OP_REMAINING", "900")
    return token


def test_changed_reference_does_not_return_the_old_webhook(lc: Any, sandbox: Sandbox, live_op: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_A)
    assert lc.resolve_webhook(str(LIB_DIR), REF_A, str(live_op)) == WEBHOOK_A
    # The operator points cmd_center_changelog_op_reference at another item.
    # The cache for REF_A is fresh, but it belongs to a different reference.
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_B)
    assert lc.resolve_webhook(str(LIB_DIR), REF_B, str(live_op)) == WEBHOOK_B
    assert [line for line in sandbox.op_argv() if line.startswith("read")] == [f"read {REF_A}", f"read {REF_B}"]


def test_same_reference_still_hits_its_cache(lc: Any, sandbox: Sandbox, live_op: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_A)
    assert lc.resolve_webhook(str(LIB_DIR), REF_A, str(live_op)) == WEBHOOK_A
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_B)
    assert lc.resolve_webhook(str(LIB_DIR), REF_B, str(live_op)) == WEBHOOK_B
    assert lc.resolve_webhook(str(LIB_DIR), REF_A, str(live_op)) == WEBHOOK_A
    assert lc.resolve_webhook(str(LIB_DIR), REF_B, str(live_op)) == WEBHOOK_B
    assert len([line for line in sandbox.op_argv() if line.startswith("read")]) == 2, "later lookups are cache hits"


def test_cache_slug_is_a_tag_never_the_raw_reference(lc: Any, sandbox: Sandbox, live_op: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_A)
    lc.resolve_webhook(str(LIB_DIR), REF_A, str(live_op))
    monkeypatch.setenv("FAKE_OP_VALUE", WEBHOOK_B)
    lc.resolve_webhook(str(LIB_DIR), REF_B, str(live_op))
    slugs = cache_slugs(sandbox)
    assert slugs == sorted([expected_slug(REF_A), expected_slug(REF_B)])
    for name in [p.name for p in sandbox.cache.iterdir()]:
        assert not re.search(r"(?i)op:|infrastructure|rmmf24|changelog-channel|webhook_url/|channel-|discord\.com", name)
    for slug in slugs:
        assert re.fullmatch(r"discord_changelog_webhook_url\.[0-9a-f]{16}", slug)


def test_untaggable_reference_fails_closed(lc: Any, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    lc.setup_logging(sandbox.state, to_stderr=False)
    sandbox.seed_webhook(WEBHOOK_A)

    class BrokenHash:
        def hexdigest(self) -> str:
            return "not-hex!"

    monkeypatch.setattr(lc.hashlib, "sha256", lambda data: BrokenHash())
    assert lc.resolve_webhook(str(LIB_DIR), REF_A, "") is None
    assert "not posting" in sandbox.log_text()
    assert sandbox.op_argv() == []
    assert REF_A not in sandbox.log_text()


# ---------------------------------------------------------------------------
# Review fix 3: a result that hits the gh --limit cap is not complete
# ---------------------------------------------------------------------------

def capped_search_runner(lc: Any, rows: list[dict[str, Any]], calls: list[str] | None = None):
    """Behaves like gh: honours the search range and --limit, and returns rows
    in an order unrelated to the timestamp (GitHub search does not sort by
    merge time), so the caller cannot page by "oldest row seen"."""
    def runner(argv: list[str], timeout: int) -> str:
        search = argv[argv.index("--search") + 1]
        limit = int(argv[argv.index("--limit") + 1])
        if calls is not None:
            calls.append(search)
        span = search.split(":", 1)[1]
        if span.startswith(">="):
            low, high = lc.parse_ts(span[2:]), None
        else:
            low_text, high_text = span.split("..")
            low, high = lc.parse_ts(low_text), lc.parse_ts(high_text)
        hits = [r for r in rows if lc.parse_ts(r["mergedAt"]) >= low
                and (high is None or lc.parse_ts(r["mergedAt"]) <= high)]
        hits.sort(key=lambda r: (r["number"] * 7919) % 101)
        return json.dumps(hits[:limit])
    return runner


def test_capped_results_are_split_until_complete(lc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "GH_LIMIT", 5)
    start, end = T0 - dt.timedelta(days=7), T0
    rows = [pr_row(n, start + dt.timedelta(minutes=97 * n)) for n in range(1, 61)]
    calls: list[str] = []
    failed: list[str] = []
    capped = capped_search_runner(lc, rows, calls)
    items = lc.collect([ANSIBLE], start, end,
                       lambda argv, timeout: capped(argv, timeout) if argv[1] == "pr" else "[]", failed)
    assert sorted(it.number for it in items if it.kind == "pr_merged") == list(range(1, 61))
    assert failed == []
    assert any(".." in c for c in calls), "a capped result must be re-queried over narrower windows"


def test_cap_that_cannot_be_split_marks_the_run_incomplete(lc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "GH_LIMIT", 3)
    same = T0 - dt.timedelta(hours=1)
    rows = [pr_row(n, same) for n in range(1, 8)]
    capped = capped_search_runner(lc, rows)
    failed: list[str] = []
    lc.collect([ANSIBLE], T0 - dt.timedelta(days=1), T0,
               lambda argv, timeout: capped(argv, timeout) if argv[1] == "pr" else "[]", failed)
    assert failed == [f"{ANSIBLE}:pr_merged"]


def test_capped_daily_run_does_not_advance_the_boundary(daily: DailyHarness, lc: Any,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    first = T0 - dt.timedelta(hours=24)
    assert daily.run(first, {}) == (0, [])
    assert lc.load_daily_end(daily.box.state) == first
    monkeypatch.setattr(lc, "GH_LIMIT", 2)
    rows = [pr_row(n, T0 - dt.timedelta(hours=3)) for n in range(1, 4)]
    capped = capped_search_runner(lc, rows)
    monkeypatch.setattr(lc, "utcnow", lambda: T0)
    monkeypatch.setattr(lc, "run_gh", lambda argv, timeout: capped(argv, timeout)
                        if argv[1] == "pr" and argv[argv.index("--repo") + 1] == ANSIBLE else "[]")
    monkeypatch.setattr(lc.urllib.request, "urlopen", FakeOpener())
    lc.main(["daily", "--no-llm"])
    assert lc.load_daily_end(daily.box.state) == first, \
        "a capped, incomplete collection must not move the boundary"



# ---------------------------------------------------------------------------
# Review fix 4: the quota pre-flight needs the token; state per message
# ---------------------------------------------------------------------------

def test_spent_quota_with_token_only_in_the_file_never_reads(lc: Any, sandbox: Sandbox,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """The systemd unit exports only HOME and PATH. The token comes from the
    file, so the pre-flight must see it, or it cannot read the quota."""
    lc.setup_logging(sandbox.state, to_stderr=False)
    sandbox.seed_webhook(age_secs=10 * 86400)
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv("FAKE_OP_REMAINING", "10")
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(token)) == FAKE_WEBHOOK
    assert "service-account ratelimit" in sandbox.op_argv()
    assert not any(line.startswith("read") for line in sandbox.op_argv())


def test_unreadable_quota_never_refreshes_the_cache(lc: Any, sandbox: Sandbox,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """The changelog is not worth spending quota blind: an unknown quota
    fails closed to the stale cache instead of the wrappers' fail open."""
    lc.setup_logging(sandbox.state, to_stderr=False)
    sandbox.seed_webhook(age_secs=10 * 86400)
    token = sandbox.root / "token"
    token.write_text("fake-token")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("FAKE_OP_REMAINING", raising=False)
    assert lc.resolve_webhook(str(LIB_DIR), lc.DEFAULT_OP_REFERENCE, str(token)) == FAKE_WEBHOOK
    assert not any(line.startswith("read") for line in sandbox.op_argv())


def test_each_delivered_message_is_persisted_before_the_next_post(lc: Any, sandbox: Sandbox) -> None:
    """A SIGTERM (TimeoutStartSec) or crash does not run `finally`, so the keys
    of a delivered message must already be on disk when the next post starts."""
    sandbox.seed_webhook()
    stamp = iso(now() - dt.timedelta(minutes=5))
    rows = {"pr:merged:mithr4ndir/ansible-quasarlab": [
        {"number": n, "title": "x" * 140, "url": "", "body": "", "mergedAt": stamp} for n in range(1, 101)
    ]}
    opener = FakeOpener()
    on_disk_at_post: list[int] = []

    def observing(request: Any, timeout: float | None = None) -> FakeResponse:
        path = sandbox.state / "posted.json"
        on_disk_at_post.append(len(json.loads(path.read_text())["posted"]) if path.exists() else 0)
        return opener(request, timeout)

    cfg = config(lc, sandbox)
    sent = lc.run_changelog(cfg, now() - dt.timedelta(hours=1), now() + dt.timedelta(minutes=1), dry_run=False,
                            use_llm=False, runner=fake_runner(rows), opener=observing, sleep=lambda s: None)
    assert sent >= 2
    assert on_disk_at_post[0] == 0
    assert all(later > earlier for earlier, later in zip(on_disk_at_post, on_disk_at_post[1:])), on_disk_at_post
