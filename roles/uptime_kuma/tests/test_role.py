"""Static and rendering tests for roles/uptime_kuma.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 --with ansible-core==2.16.3 \
        pytest roles/uptime_kuma/tests -rs

No playbook is run. Templates are rendered with Ansible's own Templar against
the role defaults (the same lazy, recursive variable resolution a play uses),
and the output is parsed as what it claims to be: compose YAML, AutoKuma
JSON, systemd units. Where the role hands arguments to one of its own scripts
(the probe unit's ExecStart, the ensure-sentinel task, the verifier task), the
rendered arguments are fed to that script's real argument parser, so a flag
renamed on one side fails here instead of on vm117.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.plugins.loader import init_plugin_loader  # noqa: E402
from ansible.template import Templar  # noqa: E402

init_plugin_loader()

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
FAKE_WEBHOOK = "https://discord.com/api/webhooks/100000000000000001/fake-token_value-for-tests-only"
FAKE_TOKEN = "Tok3nTok3nTok3nTok3nTok3nTok3n12"
DIGEST_RE = re.compile(r"^[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def load_script(name: str) -> Any:
    path = ROLE / "files" / name
    modname = name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    loader.exec_module(module)
    return module


probe = load_script("kuma-nfs-probe")
verifier = load_script("kuma-verify-entities")


def defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def host_vars(**extra: Any) -> dict[str, Any]:
    variables = defaults()
    variables.update({
        "ansible_host": "192.168.1.129",
        "ansible_check_mode": False,
        "ansible_facts": {"architecture": "x86_64", "distribution_release": "bookworm"},
        "uptime_kuma_discord_webhook_url": FAKE_WEBHOOK,
        "uptime_kuma_nfs_push_token": FAKE_TOKEN,
        "uptime_kuma_bind_address": "127.0.0.1",
        "role_path": str(ROLE),
    })
    variables.update(extra)
    return variables


def render_text(text: str, variables: dict[str, Any]) -> Any:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(text, preserve_trailing_newlines=True, escape_backslashes=False)


def render_file(name: str, **extra: Any) -> str:
    return render_text((ROLE / "templates" / name).read_text(), host_vars(**extra))


def entities(**extra: Any) -> dict[str, dict]:
    return json.loads(render_file("entities.json.j2", **extra))


def tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def find_task(items: list[dict], name: str) -> dict:
    for task in items:
        if task.get("name") == name:
            return task
        for key in ("block", "rescue", "always"):
            if key in task:
                try:
                    return find_task(task[key], name)
                except KeyError:
                    pass
    raise KeyError(name)


# ---------------------------------------------------------------------------
# YAML and file references
# ---------------------------------------------------------------------------

YAML_FILES = [
    *sorted((ROLE).rglob("*.yml")),
    REPO / "playbooks" / "uptime-kuma.yml",
    REPO / "host_vars" / "uptime-kuma" / "vars.yml",
]


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_yaml_parses(path: Path) -> None:
    assert yaml.safe_load(path.read_text()) is not None


def test_every_referenced_task_template_and_file_exists() -> None:
    text = "\n".join(p.read_text() for p in (ROLE / "tasks").glob("*.yml"))
    for ref in re.findall(r"(?:import_tasks|include_tasks):\s*(\S+)", text):
        assert (ROLE / "tasks" / ref).is_file(), ref
    for ref in re.findall(r"src:\s*(\S+\.j2)\b", text):
        assert (ROLE / "templates" / ref).is_file(), ref
    for ref in re.findall(r"src:\s*([A-Za-z0-9_.-]+)\s*$", text, re.M):
        if not ref.endswith(".j2") and "{" not in ref:
            assert (ROLE / "files" / ref).is_file(), ref
    # Loop-rendered unit templates.
    for unit in ("kuma-nfs-probe.service", "kuma-nfs-probe.timer"):
        assert (ROLE / "templates" / f"{unit}.j2").is_file()
    assert (REPO / "roles" / "cmd_center" / "files" / "apt_pinned_key.py").is_file()


def test_playbook_uses_the_role_on_the_static_group() -> None:
    [play] = yaml.safe_load((REPO / "playbooks" / "uptime-kuma.yml").read_text())
    assert play["hosts"] == "uptime_kuma"
    assert play["roles"] == ["uptime_kuma"]
    resolve = find_task(play["pre_tasks"], "Resolve the Discord webhook from env")
    assert resolve.get("no_log") is True


# ---------------------------------------------------------------------------
# compose.yaml
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0"])
def test_compose_renders_hardened_and_pinned(bind: str) -> None:
    text = render_file("compose.yaml.j2", uptime_kuma_bind_address=bind)
    compose = yaml.safe_load(text)
    kuma, autokuma = compose["services"]["uptime-kuma"], compose["services"]["autokuma"]

    assert kuma["ports"] == [f"{bind}:3001:3001"]
    assert "ports" not in autokuma
    for svc in (kuma, autokuma):
        assert DIGEST_RE.match(svc["image"]), svc["image"]
        uid = int(str(svc["user"]).split(":")[0])
        assert uid != 0
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert svc["restart"] == "unless-stopped"
        assert svc["group_add"] == ["3001"]
        assert svc["secrets"] == ["kuma_admin_password"]
        assert "mem_limit" in svc and "pids_limit" in svc
        assert svc["logging"]["options"]["max-size"] == "10m"
        for volume in svc.get("volumes", []):
            assert "docker.sock" not in volume

    assert kuma["image"].startswith("louislam/uptime-kuma:2.5.5-slim-rootless@")
    assert kuma["environment"]["UPTIME_KUMA_DB_TYPE"] == "sqlite"
    assert kuma["volumes"] == ["/opt/uptime-kuma/data:/app/data"]

    env = autokuma["environment"]
    assert env["AUTOKUMA__KUMA__URL"] == "http://uptime-kuma:3001"
    assert env["AUTOKUMA__KUMA__PASSWORD"] == "@/run/secrets/kuma_admin_password"
    assert env["AUTOKUMA__DOCKER__ENABLED"] == "false"
    assert env["AUTOKUMA__KUBERNETES__ENABLED"] == "false"
    assert env["AUTOKUMA__ON_DELETE"] == "delete"
    assert "/opt/uptime-kuma/monitors:/monitors:ro" in autokuma["volumes"]

    assert compose["secrets"]["kuma_admin_password"]["file"] == "/opt/uptime-kuma/secrets/admin_password"
    assert FAKE_WEBHOOK not in text and FAKE_TOKEN not in text


def test_bootstrap_marker_lives_in_kuma_data_dir() -> None:
    # Wiping Kuma's data must also put it back behind loopback.
    marker = find_task(tasks("kuma.yml"), "Check whether the Kuma admin account was already bootstrapped")
    path = render_text(marker["ansible.builtin.stat"]["path"], host_vars())
    assert path.startswith("/opt/uptime-kuma/data/")


def test_lan_rebind_block_does_not_test_the_variable_it_changes() -> None:
    # A block's `when` is re-evaluated per task; if it tested
    # uptime_kuma_bind_address, the include after the set_fact would be skipped.
    block = find_task(tasks("kuma.yml"), "Open the Kuma UI to the LAN now that the admin account exists")
    assert not any("uptime_kuma_bind_address" in cond for cond in block["when"])
    assert [t["name"] for t in block["block"]] == [
        "Publish Kuma on the LAN address", "Recreate Kuma with the LAN binding"]


def test_bootstrap_never_hands_the_password_to_ansible() -> None:
    task = find_task(tasks("kuma.yml"), "Create the Kuma admin account if missing and prove the login works")
    assert "environment" not in task
    assert "password" not in task["ansible.builtin.shell"]["cmd"].lower()
    rendered = render_text(task["ansible.builtin.shell"]["cmd"], host_vars())
    assert rendered.startswith("docker exec -i -e KUMA_ADMIN_USERNAME=admin uptime-kuma node - < ")


# ---------------------------------------------------------------------------
# AutoKuma entities
# ---------------------------------------------------------------------------

KNOWN_FIELDS = {
    "http": {"type", "name", "description", "url", "method", "interval", "retry_interval",
             "max_retries", "timeout", "resend_interval", "ignore_tls", "max_redirects",
             "accepted_statuscodes", "notification_name_list"},
    "push": {"type", "name", "description", "push_token", "interval", "retry_interval",
             "max_retries", "resend_interval", "notification_name_list"},
    "notification": {"type", "name", "active", "isDefault", "config"},
}


def test_entities_cover_every_endpoint_in_the_brief() -> None:
    ents = entities()
    urls = {e["url"] for e in ents.values() if e["type"] == "http"}
    assert urls == {
        "http://192.168.1.230:9090/-/ready",
        "http://192.168.1.233:9093/-/ready",
        "http://192.168.1.229/api/health",
        "http://192.168.1.231:3100/ready",
        "https://192.168.1.90:6443/readyz",
        "https://192.168.1.89:6443/readyz",
        "https://192.168.1.91:6443/readyz",
    }
    [push] = [e for e in ents.values() if e["type"] == "push"]
    assert "192.168.1.15:/mnt/tank/k8s" in push["name"]


def test_entities_are_well_formed_for_autokuma() -> None:
    ents = entities()
    names = [e["name"] for e in ents.values()]
    assert len(names) == len(set(names))
    for key, ent in ents.items():
        assert re.match(r"^[a-z0-9-]+$", key), key
        assert set(ent) <= KNOWN_FIELDS[ent["type"]], (key, set(ent) - KNOWN_FIELDS[ent["type"]])
        if ent["type"] != "notification":
            assert ent["notification_name_list"] == ["discord-alerts"]
            assert 20 <= ent["interval"] <= 3600
    notif = ents["discord-alerts"]
    assert notif["config"]["type"] == "discord"
    assert notif["config"]["discordWebhookUrl"] == FAKE_WEBHOOK
    assert notif["config"]["name"] == notif["name"]
    push = ents["nfs-tank-k8s"]
    assert re.match(r"^[A-Za-z0-9]{32}$", push["push_token"])
    assert push["interval"] > defaults()["uptime_kuma_nfs_probe_every"]
    assert all(ents[k]["ignore_tls"] for k in ents if k.startswith("k8s-api-"))
    assert not any(ents[k]["ignore_tls"] for k in ("prometheus", "alertmanager", "grafana", "loki"))


def test_expected_monitor_names_match_the_entities() -> None:
    expected = render_text("{{ uptime_kuma_expected_monitor_names }}", host_vars())
    assert sorted(expected) == sorted(e["name"] for e in entities().values()
                                      if e["type"] != "notification")


def test_disabling_the_nfs_probe_drops_its_monitor() -> None:
    ents = entities(uptime_kuma_nfs_probe_enabled=False)
    assert "nfs-tank-k8s" not in ents
    expected = render_text("{{ uptime_kuma_expected_monitor_names }}",
                           host_vars(uptime_kuma_nfs_probe_enabled=False))
    assert not any("NFS" in n for n in expected)


def test_json_special_characters_in_values_stay_json() -> None:
    ents = entities(uptime_kuma_discord_username='Amon "Hen" \\ </script>')
    assert ents["discord-alerts"]["config"]["discordUsername"] == 'Amon "Hen" \\ </script>'


def test_secret_entity_writes_are_no_log() -> None:
    write = find_task(tasks("monitors.yml"), "Write the AutoKuma entity files")
    assert write["no_log"] is True
    build = find_task(tasks("monitors.yml"), "Build the managed Kuma entities")
    assert build["no_log"] is True


# ---------------------------------------------------------------------------
# NFS probe units and task arguments
# ---------------------------------------------------------------------------

def unit_exec_args(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    [line] = [ln for ln in joined.splitlines() if ln.startswith("ExecStart=")]
    return shlex.split(line[len("ExecStart="):])


def test_probe_unit_arguments_are_accepted_by_the_probe() -> None:
    text = render_file("kuma-nfs-probe.service.j2")
    argv = unit_exec_args(text)
    assert argv[0] == "/usr/local/bin/kuma-nfs-probe"
    args = probe.parse_args(argv[1:])
    assert args.mode == "probe"
    target = probe.build_target(args)
    assert target.url() == "nfs://192.168.1.15/mnt/tank/k8s/.uptime-kuma-nfs-probe?version=3&uid=0&gid=0"
    assert probe.PUSH_BASE_RE.match(args.push_base)
    assert args.push_token_file is None  # comes from LoadCredential
    assert "LoadCredential=push_token:/opt/uptime-kuma/secrets/nfs_push_token" in text


def test_probe_unit_is_bounded_and_unprivileged() -> None:
    text = render_file("kuma-nfs-probe.service.j2")
    timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", text, re.M).group(1))
    probe_timeout = float(probe.parse_args(unit_exec_args(text)[1:]).timeout)
    # Outer bound must exceed the script's worst case: deadline, two kill
    # grace periods and the push timeout.
    assert timeout > probe_timeout + 2 * probe.KILL_GRACE_SECS + probe.PUSH_TIMEOUT_SECS
    for line in ("DynamicUser=yes", "NoNewPrivileges=yes",
                 "AmbientCapabilities=CAP_NET_BIND_SERVICE",
                 "CapabilityBoundingSet=CAP_NET_BIND_SERVICE", "ProtectSystem=strict"):
        assert re.search(rf"^{re.escape(line)}$", text, re.M), line
    assert not re.search(r"^User=", text, re.M)


def test_probe_timer_fires_more_often_than_the_heartbeat_window() -> None:
    text = render_file("kuma-nfs-probe.timer.j2")
    every = int(re.search(r"^OnUnitActiveSec=(\d+)s$", text, re.M).group(1))
    assert every < defaults()["uptime_kuma_nfs_probe_heartbeat"]
    assert "WantedBy=timers.target" in text


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_units_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    for name in ("kuma-nfs-probe.service", "kuma-nfs-probe.timer"):
        text = render_file(f"{name}.j2")
        # Point ExecStart at a binary that exists here; the rest is verified as is.
        text = text.replace("/usr/local/bin/kuma-nfs-probe", shutil.which("true") or "/bin/true")
        (tmp_path / name).write_text(text)
    proc = subprocess.run(["systemd-analyze", "verify", "--man=no",
                           str(tmp_path / "kuma-nfs-probe.service"),
                           str(tmp_path / "kuma-nfs-probe.timer")],
                          capture_output=True, text=True, timeout=60, check=False)
    problems = [ln for ln in proc.stderr.splitlines()
                if "kuma-nfs-probe" in ln and "Command" not in ln]
    assert proc.returncode == 0 and not problems, proc.stderr


def test_ensure_sentinel_task_arguments_are_accepted_by_the_probe() -> None:
    task = find_task(tasks("nfs_probe.yml"), "Create the NFS sentinel on the export if it is missing")
    argv = [str(render_text(a, host_vars())) for a in task["ansible.builtin.command"]["argv"]]
    args = probe.parse_args(argv[1:])
    assert args.mode == "ensure-sentinel"
    probe.build_target(args)


def test_verifier_task_arguments_are_accepted_by_the_verifier() -> None:
    task = find_task(tasks("verify.yml"), "Wait until Kuma holds every managed monitor, wired to Discord")
    argv = render_text(task["ansible.builtin.command"]["argv"], host_vars())
    assert argv[0] == "/usr/local/bin/kuma-verify-entities"
    captured: list[list[str]] = []

    def fake_cli(args: list[str]) -> Any:
        captured.append(list(args))
        return {} if args[0] == "monitor" else []

    assert verifier.main(argv[1:], cli=fake_cli) == 1  # nothing exists yet
    monitors = [a.split("=", 1)[1] for a in argv[1:] if a.startswith("--monitor=")]
    assert sorted(monitors) == sorted(e["name"] for e in entities().values()
                                      if e["type"] != "notification")
    retries = int(render_text(task["retries"], host_vars()))
    assert retries * task["delay"] >= 3 * defaults()["uptime_kuma_autokuma_sync_interval"]


# ---------------------------------------------------------------------------
# kuma-verify-entities logic
# ---------------------------------------------------------------------------

NOTIF = "Discord #alerts (direct, not via discord-alert-proxy)"


def monitor(name: str, active: bool = True, wired: dict | None = None) -> dict:
    return {"name": name, "active": active,
            "notificationIDList": {"7": True} if wired is None else wired}


@pytest.mark.parametrize("monitors, notifications, expect", [
    ({"1": monitor("A"), "2": monitor("B")}, [{"id": 7, "name": NOTIF}], {"ok": True}),
    ({"1": monitor("A")}, [{"id": 7, "name": NOTIF}], {"ok": False, "missing": ["B"]}),
    ({"1": monitor("A"), "2": monitor("B"), "3": monitor("B")}, [{"id": 7, "name": NOTIF}],
     {"ok": False, "duplicated": ["B"]}),
    ({"1": monitor("A"), "2": monitor("B", active=False)}, [{"id": 7, "name": NOTIF}],
     {"ok": False, "inactive": ["B"]}),
    ({"1": monitor("A"), "2": monitor("B", wired={"7": False})}, [{"id": 7, "name": NOTIF}],
     {"ok": False, "not_notifying": ["B"]}),
    ({"1": monitor("A"), "2": monitor("B")}, [{"id": 7, "name": "something else"}],
     {"ok": False, "notification_missing": True}),
    # Unmanaged monitors made in the UI are fine; they are not ours to judge.
    ({"1": monitor("A"), "2": monitor("B"), "9": monitor("hand made", active=False)},
     [{"id": 7, "name": NOTIF}], {"ok": True}),
])
def test_verifier_findings(monitors: dict, notifications: list, expect: dict,
                           capsys: pytest.CaptureFixture[str]) -> None:
    def fake_cli(args: list[str]) -> Any:
        return monitors if args[0] == "monitor" else notifications

    rc = verifier.main(["--notification", NOTIF, "--monitor", "A", "--monitor", "B"], cli=fake_cli)
    out = json.loads(capsys.readouterr().out)
    for key, value in expect.items():
        assert out[key] == value, out
    assert rc == (0 if expect["ok"] else 1)


def test_verifier_reports_when_kuma_cannot_be_asked(capsys: pytest.CaptureFixture[str]) -> None:
    def failing_cli(args: list[str]) -> Any:
        raise RuntimeError("kuma monitor list exited 1: LoginError")

    assert verifier.main(["--notification", NOTIF, "--monitor", "A"], cli=failing_cli) == 2
    assert "LoginError" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# Defaults that encode decisions
# ---------------------------------------------------------------------------

def test_images_are_digest_pinned_and_not_kuma_1() -> None:
    d = defaults()
    for key in ("uptime_kuma_image", "uptime_kuma_autokuma_image"):
        assert DIGEST_RE.match(d[key]), d[key]
    assert not d["uptime_kuma_image"].split(":")[1].startswith("1")


def test_no_webhook_or_token_in_defaults_or_host_vars() -> None:
    text = (ROLE / "defaults" / "main.yml").read_text() + \
        (REPO / "host_vars" / "uptime-kuma" / "vars.yml").read_text()
    assert "discord.com/api/webhooks/" not in text
    assert defaults()["uptime_kuma_discord_webhook_url"] == ""


@pytest.mark.parametrize("stdout, rc, failed", [
    ("5.5.1", 0, False),        # Docker's bookworm repo, 2026-09
    ("v2.29.1", 0, False),
    ("2.18.0", 0, False),
    ("10.0.0", 0, False),
    ("1.29.2", 0, True),        # Debian's docker-compose v1
    ("", 0, True),
    ("5.5.1", 1, True),         # "docker: 'compose' is not a docker command"
])
def test_compose_version_gate(stdout: str, rc: int, failed: bool) -> None:
    task = find_task(tasks("docker.yml"), "Prove `docker compose` works before relying on it")
    expr = "{{ " + task["failed_when"].strip() + " }}"
    result = render_text(expr, host_vars(uptime_kuma_compose_version={"rc": rc, "stdout": stdout}))
    assert result is failed


def test_docker_key_pin_is_a_full_fingerprint() -> None:
    assert re.match(r"^[0-9A-F]{40}$", defaults()["uptime_kuma_docker_apt_key_fingerprint"])
