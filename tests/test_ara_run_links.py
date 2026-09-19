"""Tests for the ARA run links in scripts/run-proxmox.sh and run-security.sh.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 --with ansible-core==2.16.3 pytest tests/ -rs

What these cover, in order of what would hurt most if it broke:

1. Fail open. ARA down, hanging, erroring or missing a record must still leave
   ansible_run_success and ansible_playbook_changed_tasks in the .prom file with
   the right values. The link is a convenience; the metrics are the monitoring.
2. Correlation. The link must point at THIS run of the playbook. The fake ARA
   below holds an overlapping, newer run of the same playbook under a different
   label, which is exactly what a manual run racing a timer looks like, and the
   tests assert the wrapper links its own run, not the newest one.
3. Format. Whatever lands in the .prom file has to be parseable exposition and
   has to be written by the same atomic rename as the rest of the metrics.

SAFETY: nothing here can reach 1Password, Proxmox, the real ARA or any other
host. Every wrapper process runs with PATH set to a sandbox bin directory
holding fakes for op, ansible-playbook, ansible-inventory, ansible-vault, curl
and logger; the fake ARA is an http.server bound to 127.0.0.1 on a random port.
The automation checkout is a real clone of a throwaway local origin holding the
real scripts, so the wrapper under test is this repo's code, not a re-write.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib" / "ara-run-links.sh"
HELPER = REPO / "scripts" / "lib" / "ara-run-links.py"

TOOLS = [
    "awk", "basename", "bash", "cat", "chmod", "cut", "date", "dirname", "env",
    "find", "flock", "git", "grep", "head", "ls", "mkdir", "mktemp", "mv",
    "paste", "python3", "readlink", "rm", "sed", "sh", "sleep", "sort", "stat",
    "tail", "tee", "timeout", "touch", "tr", "wc", "xargs",
]

COPIED = [
    ".gitignore",
    "scripts/run-proxmox.sh",
    "scripts/run-security.sh",
    "scripts/resolve-inventory.sh",
    "scripts/vault-pass.sh",
    "scripts/lib/sync-repo.sh",
    "scripts/lib/op-killswitch.sh",
    "scripts/lib/op-secret-cache.sh",
    "scripts/lib/proxmox-vault.sh",
    "scripts/lib/ara-run-links.sh",
    "scripts/lib/ara-run-links.py",
]

PROXMOX_PLAYBOOKS = [
    "proxmox.yml", "vm_baseline.yml", "monitoring.yml", "jellyfin.yml",
    "authentik.yml", "lb_setup.yml", "deploy-ha.yml",
]
SECURITY_PLAYBOOKS = ["wazuh.yml", "crowdsec.yml"]

INFO_RE = re.compile(
    r'^ansible_playbook_last_run_info\{playbook="(?P<playbook>[^"]+)",ara_url="(?P<url>[^"]+)"\} 1$',
    re.MULTILINE,
)

FAKE_OP = r"""#!/bin/bash
if [[ "$1 $2" == "service-account ratelimit" ]]; then
    echo "TYPE       ACTION        LIMIT    USED    REMAINING    RESET"
    echo "account    read_write    1000     100     900          N/A"
    exit 0
fi
[[ "$1" == "read" ]] && { printf '%s' "fake-secret"; exit 0; }
exit 1
"""

FAKE_ANSIBLE_VAULT = r"""#!/bin/bash
pass_file=""
while [[ $# -gt 0 ]]; do
    [[ "$1" == "--vault-password-file" ]] && pass_file="$2"
    shift
done
"$pass_file" > /dev/null || exit 1
echo "vault_proxmox_api_token: fake-proxmox-token"
"""

# Stands in for the ARA callback: records the playbook and the labels the
# wrapper handed it, the way the callback records a playbook and its labels.
FAKE_ANSIBLE_PLAYBOOK = r"""#!/bin/bash
scripts/vault-pass.sh > /dev/null || { echo "vault-pass failed"; exit 1; }
python3 - "$1" <<'PY' >> "$ARA_REGISTRY"
import json, os, sys
print(json.dumps({
    "path": os.path.abspath(sys.argv[1]),
    "labels": [l for l in os.environ.get("ARA_DEFAULT_LABELS", "").split(",") if l],
}))
PY
echo "PLAY RECAP *********************************************************************"
if [[ "${FAKE_PLAYBOOK_RC:-0}" -ne 0 ]]; then
    echo "pve                        : ok=3    changed=0    unreachable=0    failed=1    skipped=0"
    exit "${FAKE_PLAYBOOK_RC}"
fi
echo "pve                        : ok=9    changed=2    unreachable=0    failed=0    skipped=0"
"""

# 000 keeps resolve-inventory.sh on its cached-inventory path: no Proxmox API,
# no dynamic inventory, no 1Password.
FAKE_CURL = "#!/bin/bash\nprintf '%s' '000'\n"

STUB_SYNC_TARGETS = "#!/usr/bin/env bash\necho 'stub sync-prometheus-targets'\n"


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def free_port() -> int:
    """A port nothing listens on, for the ARA-is-down cases."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeAra:
    """Minimal stand-in for the ARA 1.7.2 playbook list endpoint.

    Answers /api/v1/playbooks?label=X out of a registry file the fake
    ansible-playbook appends to, with ids assigned in insertion order. Label
    matching is case-insensitive exact, like ARA's labels__name__iexact.
    """

    def __init__(self, registry: Path) -> None:
        self.registry = registry
        self.mode = "ok"          # ok | hang | error | garbage
        self.hang_secs = 30
        self.queries: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                server.queries.append(self.path)
                if parsed.path != "/api/v1/playbooks":
                    self.send_error(404)
                    return
                if server.mode == "hang":
                    time.sleep(server.hang_secs)
                    return
                if server.mode == "error":
                    self.send_error(500)
                    return
                body = (b"not json at all" if server.mode == "garbage"
                        else json.dumps(server.payload(query.get("label", [""])[0])).encode())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def records(self) -> list[dict]:
        if not self.registry.exists():
            return []
        return [json.loads(line) for line in self.registry.read_text().splitlines() if line.strip()]

    def add(self, path: str, labels: list[str]) -> None:
        """Record a playbook run that this test did not launch."""
        with self.registry.open("a") as handle:
            handle.write(json.dumps({"path": path, "labels": labels}) + "\n")

    def payload(self, label: str) -> dict:
        results = [
            {"id": 1000 + index, "path": record["path"],
             "labels": [{"name": name} for name in record["labels"]]}
            for index, record in enumerate(self.records())
            if any(name.lower() == label.lower() for name in record["labels"])
        ]
        return {"count": len(results), "next": None, "previous": None, "results": results}

    def id_of(self, path_suffix: str, label: str) -> int:
        for index, record in enumerate(self.records()):
            if record["path"].endswith(path_suffix) and label in record["labels"]:
                return 1000 + index
        raise AssertionError(f"no record for {path_suffix} under {label}")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ara-run-links-test-"))
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
        self._script("ansible-inventory", "#!/bin/bash\nexit 1\n")
        self._script("ansible-playbook", FAKE_ANSIBLE_PLAYBOOK)
        self._script("curl", FAKE_CURL)

        self.registry = self.tmp / "ara-registry.jsonl"
        self.registry.touch()
        self.ara = FakeAra(self.registry)
        self.addCleanup(self.ara.stop)

        self.repo = self._make_automation_checkout()
        self.home = self.tmp / "home"
        (self.home / ".config" / "op").mkdir(parents=True)
        (self.home / ".config" / "op" / "service-account-token").write_text("dummy-not-a-token")
        self.textfiles = self.tmp / "textfiles"
        self.logs = self.tmp / "logs"
        self.cache = self.tmp / "secrets"

        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "ANSIBLE_AUTOMATION_REPO_DIR": str(self.repo),
            "ANSIBLE_LOG_DIR": str(self.logs),
            "ANSIBLE_TEXTFILE_DIR": str(self.textfiles),
            "ARA_ENV_FILE": str(self.tmp / "no-ara-env.sh"),
            "ARA_BASE_URL": self.ara.url,
            "ARA_REGISTRY": str(self.registry),
            "OP_KILLSWITCH_STATE_DIR": str(self.tmp / "state"),
            "OP_KILLSWITCH_METRIC_FILE": str(self.textfiles / "killswitch.prom"),
            "OP_SECRET_CACHE_DIR": str(self.cache),
        }

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body if body.startswith("#!") else "#!/bin/bash\n" + body)
        path.chmod(0o755)

    def _make_automation_checkout(self) -> Path:
        work = self.tmp / "work"
        for rel in COPIED:
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        # Out of scope here and it would need kubectl; the security wrapper only
        # logs its output.
        stub = work / "scripts" / "sync-prometheus-targets.sh"
        stub.write_text(STUB_SYNC_TARGETS)
        stub.chmod(0o755)
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

    def seed_secret_cache(self) -> None:
        """Warm cache: no fake op read is needed during the run."""
        self.cache.mkdir(mode=0o700, exist_ok=True)
        for slug in ("ansible_vault_password", "authentik_pg_password"):
            (self.cache / slug).write_text("cached")

    def run_wrapper(self, name: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(
            [str(self.bin / "bash"), str(self.repo / "scripts" / name)],
            env=full_env, capture_output=True, text=True, timeout=120, check=False,
        )

    def prom(self, name: str) -> str:
        path = self.textfiles / name
        self.assertTrue(path.is_file(), f"{name} was not written")
        return path.read_text()

    def info_series(self, name: str) -> dict[str, str]:
        found = {}
        for line in self.prom(name).splitlines():
            match = INFO_RE.match(line)
            if match:
                found[match["playbook"]] = match["url"]
        return found

    def labels_used(self) -> set[str]:
        labels = set()
        for record in self.ara.records():
            labels.update(record["labels"])
        return labels

    def run_label(self, prefix: str) -> str:
        matching = {label for label in self.labels_used() if label.startswith(f"run:{prefix}:")}
        self.assertEqual(len(matching), 1, f"expected one {prefix} run label, got {matching}")
        return matching.pop()

    def assert_core_metrics_intact(self, prom_file: str, playbooks: list[str]) -> None:
        """The metrics that existed before ARA links, with the values a
        successful run must produce. This is the fail-open assertion."""
        text = self.prom(prom_file)
        prefix = "ansible_security_run" if prom_file.endswith("security.prom") else "ansible_run"
        self.assertIn(f"{prefix}_success 1", text)
        self.assertRegex(text, rf"{prefix}_timestamp_seconds \d{{10}}")
        self.assertIn(f'{prefix}_repo_sync_success{{repo="ansible-quasarlab"}} 1', text)
        for playbook in playbooks:
            self.assertIn(f'ansible_playbook_success{{playbook="{playbook}",failed_hosts="none"}} 1', text)
            self.assertIn(f'ansible_playbook_changed_tasks{{playbook="{playbook}",changed_hosts="pve"}} 2', text)
        self.assertFalse(list(self.textfiles.glob("*.tmp")), "temporary metrics file left behind")


class HelperTests(Sandbox):
    """The lookup helper on its own, against the fake ARA."""

    def helper(self, *args: str, base_url: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.bin / "python3"), "-I", str(HELPER),
             "--base-url", base_url or self.ara.url, "--timeout", "3", *args],
            env=self.env, capture_output=True, text=True, timeout=60, check=False,
        )

    def test_returns_the_run_carrying_the_label_not_the_newest(self) -> None:
        self.ara.add("/repo/playbooks/vm_baseline.yml", ["run:proxmox:ours"])
        # A later, unrelated run of the same playbook: a manual run overlapping
        # the timer. Higher id, so "newest wins" would pick this one.
        self.ara.add("/repo/playbooks/vm_baseline.yml", ["run:proxmox:someone-else"])
        proc = self.helper("--label", "run:proxmox:ours", "vm_baseline.yml")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines()[-1],
            f'ansible_playbook_last_run_info{{playbook="vm_baseline.yml",'
            f'ara_url="{self.ara.url}/playbooks/1000.html"}} 1',
        )

    def test_no_match_prints_nothing_and_succeeds(self) -> None:
        self.ara.add("/repo/playbooks/vm_baseline.yml", ["run:proxmox:ours"])
        proc = self.helper("--label", "run:proxmox:nobody", "vm_baseline.yml")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("no ARA record", proc.stderr)

    def test_same_playbook_twice_under_one_label_is_not_linked(self) -> None:
        self.ara.add("/repo/playbooks/wazuh.yml", ["run:security:ours"])
        self.ara.add("/repo/playbooks/wazuh.yml", ["run:security:ours"])
        proc = self.helper("--label", "run:security:ours", "wazuh.yml")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("appears more than once", proc.stderr)

    def test_partial_match_links_what_it_found(self) -> None:
        self.ara.add("/repo/playbooks/wazuh.yml", ["run:security:ours"])
        proc = self.helper("--label", "run:security:ours", "wazuh.yml", "crowdsec.yml")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(INFO_RE.findall(proc.stdout)), 1)
        self.assertIn("wazuh.yml", proc.stdout)
        self.assertNotIn("crowdsec.yml", proc.stdout)

    def test_ara_down_fails_with_a_reason_and_no_output(self) -> None:
        proc = self.helper("--label", "run:proxmox:ours", "vm_baseline.yml",
                           base_url=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("lookup failed", proc.stderr)

    def test_server_error_and_garbage_response_fail_closed_on_output(self) -> None:
        self.ara.add("/repo/playbooks/wazuh.yml", ["run:security:ours"])
        for mode in ("error", "garbage"):
            with self.subTest(mode=mode):
                self.ara.mode = mode
                proc = self.helper("--label", "run:security:ours", "wazuh.yml")
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")

    def test_rejects_values_that_could_break_out_of_a_prometheus_label(self) -> None:
        for args in (
            ["--label", 'x" evil="', "wazuh.yml"],
            ["--label", "run:x:1", 'wa"zuh.yml'],
            ["--label", "run:x:1", "wazuh.yml\nansible_playbook_last_run_info{} 1"],
        ):
            with self.subTest(args=args):
                proc = self.helper(*args)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(proc.stdout, "")

    def test_rejects_a_base_url_carrying_a_quote(self) -> None:
        proc = self.helper("--label", "run:x:1", "wazuh.yml",
                           base_url='http://127.0.0.1:8000/" evil="')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


class LibTests(Sandbox):
    """scripts/lib/ara-run-links.sh, the layer the wrappers call."""

    def bash(self, script: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(
            [str(self.bin / "bash"), "-uo", "pipefail", "-c", f'source "{LIB}"; {script}'],
            env=full_env, capture_output=True, text=True, timeout=60, check=False,
        )

    def test_tag_run_appends_one_unique_label(self) -> None:
        proc = self.bash('ara_tag_run proxmox; echo "$ARA_DEFAULT_LABELS"',
                         env={"ARA_DEFAULT_LABELS": "existing:label"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        existing, _, run_label = proc.stdout.strip().partition(",")
        self.assertEqual(existing, "existing:label")
        self.assertRegex(run_label, r"^run:proxmox:[0-9a-f-]{36}$")

        again = self.bash('ara_tag_run proxmox; echo "$ARA_DEFAULT_LABELS"')
        self.assertNotEqual(again.stdout.strip(), run_label)

    def test_metrics_are_empty_and_quiet_without_a_run_label(self) -> None:
        proc = self.bash('ara_run_link_metrics vm_baseline.yml; echo "rc=$?"')
        self.assertEqual(proc.stdout, "rc=0\n")

    def test_lookup_is_time_bounded_when_ara_hangs(self) -> None:
        self.ara.mode = "hang"
        start = time.monotonic()
        proc = self.bash('ara_tag_run proxmox; ara_run_link_metrics vm_baseline.yml; echo "rc=$?"',
                         env={"ARA_LOOKUP_TIMEOUT_SECS": "2"})
        elapsed = time.monotonic() - start
        self.assertEqual(proc.stdout, "rc=0\n")
        self.assertIn("failed or timed out", proc.stderr)
        self.assertLess(elapsed, 15, "the lookup was not time-bounded")

    def test_a_nonsense_timeout_falls_back_to_a_real_bound(self) -> None:
        # `timeout 0` means no timeout at all, which is the failure mode worth
        # guarding: a typo in the env would remove the bound entirely.
        self.ara.mode = "hang"
        start = time.monotonic()
        proc = self.bash('ara_tag_run proxmox; ara_run_link_metrics vm_baseline.yml; echo "rc=$?"',
                         env={"ARA_LOOKUP_TIMEOUT_SECS": "0"})
        self.assertEqual(proc.stdout, "rc=0\n")
        self.assertLess(time.monotonic() - start, 25)


class ProxmoxWrapperTests(Sandbox):
    """run-proxmox.sh end to end, ARA up and ARA down."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_secret_cache()

    def test_each_playbook_links_to_its_own_run_of_this_wrapper_run(self) -> None:
        # An older run of vm_baseline.yml from someone else, plus a newer one
        # appended below: neither may win.
        self.ara.add("/elsewhere/playbooks/vm_baseline.yml", ["run:manual:earlier"])
        proc = self.run_wrapper("run-proxmox.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.ara.add("/elsewhere/playbooks/vm_baseline.yml", ["run:manual:later"])

        label = self.run_label("proxmox")
        series = self.info_series("ansible_run.prom")
        self.assertEqual(sorted(series), sorted(PROXMOX_PLAYBOOKS))
        for playbook in PROXMOX_PLAYBOOKS:
            expected = self.ara.id_of(f"/playbooks/{playbook}", label)
            self.assertEqual(series[playbook], f"{self.ara.url}/playbooks/{expected}.html")
        self.assert_core_metrics_intact("ansible_run.prom", PROXMOX_PLAYBOOKS)

    def test_the_wrapper_asks_ara_for_its_own_label_only(self) -> None:
        proc = self.run_wrapper("run-proxmox.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        label = self.run_label("proxmox")
        self.assertEqual(len(self.ara.queries), 1, self.ara.queries)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.ara.queries[0]).query)
        self.assertEqual(query["label"], [label])

    def test_ara_down_keeps_every_other_metric(self) -> None:
        proc = self.run_wrapper("run-proxmox.sh",
                                env={"ARA_BASE_URL": f"http://127.0.0.1:{free_port()}"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.info_series("ansible_run.prom"), {})
        self.assert_core_metrics_intact("ansible_run.prom", PROXMOX_PLAYBOOKS)

    def test_ara_hanging_keeps_every_other_metric_and_does_not_stall_the_run(self) -> None:
        self.ara.mode = "hang"
        start = time.monotonic()
        proc = self.run_wrapper("run-proxmox.sh", env={"ARA_LOOKUP_TIMEOUT_SECS": "2"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(time.monotonic() - start, 30, "the wrapper waited on a hanging ARA")
        self.assertEqual(self.info_series("ansible_run.prom"), {})
        self.assert_core_metrics_intact("ansible_run.prom", PROXMOX_PLAYBOOKS)

    def test_a_playbook_missing_from_ara_does_not_cost_the_others_their_link(self) -> None:
        proc = self.run_wrapper("run-proxmox.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        series = self.info_series("ansible_run.prom")
        self.assertEqual(sorted(series), sorted(PROXMOX_PLAYBOOKS))

        # Second run with one playbook's record dropped from ARA.
        self.registry.write_text("")
        self.textfiles.mkdir(exist_ok=True)
        dropped = "jellyfin.yml"
        original = FAKE_ANSIBLE_PLAYBOOK.replace(
            'python3 - "$1"', f'[[ "$1" == *{dropped} ]] || python3 - "$1"')
        self._script("ansible-playbook", original)
        proc = self.run_wrapper("run-proxmox.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        series = self.info_series("ansible_run.prom")
        self.assertEqual(sorted(series), sorted(p for p in PROXMOX_PLAYBOOKS if p != dropped))
        self.assert_core_metrics_intact("ansible_run.prom", PROXMOX_PLAYBOOKS)

    def test_a_failed_playbook_still_gets_its_link(self) -> None:
        proc = self.run_wrapper("run-proxmox.sh", env={"FAKE_PLAYBOOK_RC": "2"})
        self.assertEqual(proc.returncode, 2)
        text = self.prom("ansible_run.prom")
        self.assertIn("ansible_run_success 0", text)
        self.assertIn('ansible_playbook_success{playbook="vm_baseline.yml",failed_hosts="pve"} 0', text)
        # A failed run is the one you most want to open, and the lookup has to
        # survive the non-zero exit path to produce it.
        label = self.run_label("proxmox")
        series = self.info_series("ansible_run.prom")
        self.assertEqual(sorted(series), sorted(PROXMOX_PLAYBOOKS))
        for playbook in PROXMOX_PLAYBOOKS:
            expected = self.ara.id_of(f"/playbooks/{playbook}", label)
            self.assertEqual(series[playbook], f"{self.ara.url}/playbooks/{expected}.html")

    def test_the_metrics_file_is_valid_exposition_with_one_help_per_family(self) -> None:
        proc = self.run_wrapper("run-proxmox.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        helps, types, samples = [], [], []
        for line in self.prom("ansible_run.prom").splitlines():
            if line.startswith("# HELP"):
                helps.append(line.split()[2])
            elif line.startswith("# TYPE"):
                types.append(line.split()[2])
            elif line.strip():
                self.assertRegex(line, r"^[a-z_]+(\{[^}]*\})? -?\d+$", line)
                samples.append(re.split(r"[{ ]", line)[0])
        self.assertEqual(len(helps), len(set(helps)), "duplicate HELP in one file")
        self.assertEqual(sorted(helps), sorted(types))
        self.assertIn("ansible_playbook_last_run_info", helps)
        for family in set(samples):
            self.assertIn(family, helps, f"{family} has no HELP line")

    def test_two_runs_use_different_labels_and_relink(self) -> None:
        self.assertEqual(self.run_wrapper("run-proxmox.sh").returncode, 0)
        first_label = self.run_label("proxmox")
        first = self.info_series("ansible_run.prom")["proxmox.yml"]

        self.assertEqual(self.run_wrapper("run-proxmox.sh").returncode, 0)
        labels = {l for l in self.labels_used() if l.startswith("run:proxmox:")}
        self.assertEqual(len(labels), 2, labels)
        second = self.info_series("ansible_run.prom")["proxmox.yml"]
        self.assertNotEqual(first, second)
        second_label = (labels - {first_label}).pop()
        self.assertEqual(
            second,
            f"{self.ara.url}/playbooks/{self.ara.id_of('/playbooks/proxmox.yml', second_label)}.html",
        )


class SecurityWrapperTests(Sandbox):
    """run-security.sh: same mechanism, its own metrics file."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_secret_cache()

    def test_security_playbooks_link_to_this_run(self) -> None:
        proc = self.run_wrapper("run-security.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        label = self.run_label("security")
        series = self.info_series("ansible_security.prom")
        self.assertEqual(sorted(series), sorted(SECURITY_PLAYBOOKS))
        for playbook in SECURITY_PLAYBOOKS:
            expected = self.ara.id_of(f"/playbooks/{playbook}", label)
            self.assertEqual(series[playbook], f"{self.ara.url}/playbooks/{expected}.html")
        self.assert_core_metrics_intact("ansible_security.prom", SECURITY_PLAYBOOKS)

    def test_ara_down_keeps_every_other_security_metric(self) -> None:
        proc = self.run_wrapper("run-security.sh",
                                env={"ARA_BASE_URL": f"http://127.0.0.1:{free_port()}"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.info_series("ansible_security.prom"), {})
        self.assert_core_metrics_intact("ansible_security.prom", SECURITY_PLAYBOOKS)


class SandboxGuardTests(Sandbox):
    def test_real_binaries_are_unreachable(self) -> None:
        for tool in ("op", "ansible-playbook", "ansible-vault", "ansible-inventory", "curl"):
            proc = subprocess.run(
                [str(self.bin / "bash"), "-c", f"command -v {tool}"],
                env=self.env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.stdout.strip(), str(self.bin / tool))

    def test_the_fake_ara_is_the_only_ara_in_reach(self) -> None:
        self.assertTrue(self.env["ARA_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertNotIn("192.168.", self.env["ARA_BASE_URL"])


if __name__ == "__main__":
    unittest.main()
