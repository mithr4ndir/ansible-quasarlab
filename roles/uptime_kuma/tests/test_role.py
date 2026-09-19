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
import os
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


def test_compose_publishes_only_the_tls_proxy() -> None:
    text = render_file("compose.yaml.j2")
    compose = yaml.safe_load(text)
    services = compose["services"]
    assert set(services) == {"uptime-kuma", "autokuma", "kuma-gate", "kuma-proxy"}

    # SECURITY: Kuma speaks plain HTTP, so it must not be published at all.
    published = {name: svc.get("ports") for name, svc in services.items() if svc.get("ports")}
    assert published == {"kuma-proxy": ["0.0.0.0:3001:3001"]}

    for name, svc in services.items():
        assert DIGEST_RE.match(svc["image"]), (name, svc["image"])
        assert int(str(svc["user"]).split(":")[0]) != 0, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["restart"] == "unless-stopped", name
        assert svc["group_add"] == ["3001"], name
        assert "mem_limit" in svc and "pids_limit" in svc, name
        for volume in svc.get("volumes", []):
            assert "docker.sock" not in volume, name

    proxy = services["kuma-proxy"]
    assert proxy["image"].startswith("nginxinc/nginx-unprivileged:1.30.5-alpine@")
    assert proxy["user"] == "101:101"
    assert "/opt/uptime-kuma/tls:/etc/uptime-kuma-tls:ro" in proxy["volumes"]
    assert "/opt/uptime-kuma/kuma-proxy.conf:/etc/nginx/conf.d/default.conf:ro" in proxy["volumes"]
    assert set(proxy["depends_on"]) == {"uptime-kuma", "kuma-gate"}

    gate = services["kuma-gate"]
    assert gate["image"] == services["uptime-kuma"]["image"]
    assert gate["secrets"] == ["kuma_admin_password"]
    assert gate["entrypoint"] == ["node", "/gate/kuma-gate.js"]
    assert "/opt/uptime-kuma/kuma-gate.js:/gate/kuma-gate.js:ro" in gate["volumes"]

    kuma = services["uptime-kuma"]
    assert "ports" not in kuma
    assert kuma["image"].startswith("louislam/uptime-kuma:2.5.5-slim-rootless@")
    assert kuma["environment"]["UPTIME_KUMA_DB_TYPE"] == "sqlite"
    assert kuma["volumes"] == ["/opt/uptime-kuma/data:/app/data"]
    assert kuma["secrets"] == ["kuma_admin_password"]

    autokuma = services["autokuma"]
    env = autokuma["environment"]
    assert env["AUTOKUMA__KUMA__URL"] == "http://uptime-kuma:3001"
    assert not any("PASSWORD" in k for k in env)
    assert env["XDG_CONFIG_HOME"] == "/config"
    assert env["AUTOKUMA__DOCKER__ENABLED"] == "false"
    assert env["AUTOKUMA__KUBERNETES__ENABLED"] == "false"
    assert env["AUTOKUMA__ON_DELETE"] == "delete"
    assert "/opt/uptime-kuma/secrets/autokuma.toml:/config/autokuma/config.toml:ro" in autokuma["volumes"]
    assert "secrets" not in autokuma

    assert compose["secrets"]["kuma_admin_password"]["file"] == "/opt/uptime-kuma/secrets/admin_password"
    assert FAKE_WEBHOOK not in text and FAKE_TOKEN not in text


def test_proxy_serves_tls_only_and_asks_the_gate() -> None:
    conf = render_file("kuma-proxy.conf.j2")
    assert re.findall(r"^\s*listen\s+(.*);", conf, re.M) == ["3001 ssl"]   # no plain listener
    assert "ssl_protocols       TLSv1.2 TLSv1.3;" in conf
    assert "ssl_certificate_key /etc/uptime-kuma-tls/key.pem;" in conf
    body = conf[conf.index("location / {"):]
    assert "auth_request /__kuma_gate;" in body            # websockets included
    assert "proxy_pass $kuma_upstream;" in body
    assert "proxy_set_header Upgrade $http_upgrade;" in body
    assert re.search(r"location ~ \^/setup.*\n\s*return 403;", conf)
    assert "add_header Strict-Transport-Security" in conf


def test_the_deploy_checks_tls_and_refuses_plain_http() -> None:
    items = tasks("kuma.yml")
    tls_local = find_task(items, "Prove the UI answers over TLS on the host, certificate verified")
    ca = render_text(tls_local["ansible.builtin.uri"]["ca_path"], host_vars())
    assert ca == "/opt/uptime-kuma/tls/cert.pem"
    assert tls_local["ansible.builtin.uri"]["url"].startswith("https://127.0.0.1")
    setup = find_task(items, "Prove the setup routes are refused")
    assert setup["ansible.builtin.uri"]["status_code"] == 403
    plain = find_task(items, "Prove plain HTTP on the LAN port gets no UI")
    assert plain["ansible.builtin.uri"]["url"].startswith("http://")
    assert "Uptime Kuma' in" in plain["failed_when"]


def test_exposure_is_gated_at_runtime_not_by_a_deploy_marker() -> None:
    # The old design published the LAN port once a marker file existed, so a
    # database lost while running came back exposed. Nothing may decide
    # exposure from a marker or a bind address any more.
    text = "\n".join(f.read_text() for f in sorted((ROLE / "tasks").glob("*.yml")))
    text += (ROLE / "templates" / "compose.yaml.j2").read_text()
    for gone in ("uptime_kuma_bind_address", "needs_lan_rebind"):
        assert gone not in text, gone
    gate = (ROLE / "files" / "kuma-gate.js").read_text()
    assert 'ask(socket, "needSetup"' in gate and 'ask(socket, "login"' in gate


def test_bootstrap_never_hands_the_password_to_ansible() -> None:
    task = find_task(tasks("kuma.yml"), "Create the Kuma admin account if missing and prove the login works")
    assert "environment" not in task
    assert "password" not in task["ansible.builtin.shell"]["cmd"].lower()
    rendered = render_text(task["ansible.builtin.shell"]["cmd"], host_vars())
    assert rendered == ("docker exec -i -e KUMA_ACTION=bootstrap -e KUMA_ADMIN_USERNAME=admin "
                        "uptime-kuma node - < /opt/uptime-kuma/kuma-admin.js")
    # Only "no answer yet" is retried; a refused setup or login fails at once.
    assert task["until"] == "uptime_kuma_bootstrap.rc != 3"


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
    captured: list[tuple] = []

    def fake_lister(script: str, username: str, container: str = "") -> dict:
        captured.append((script, username, container))
        return {"ok": True, "monitors": [], "notifications": []}

    assert verifier.main(argv[1:], lister=fake_lister) == 1  # nothing exists yet
    assert captured == [("/opt/uptime-kuma/kuma-admin.js", "admin", "uptime-kuma")]
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
    def fake_lister(script: str, username: str, container: str = "") -> dict:
        return {"ok": True, "monitors": monitors, "notifications": notifications}

    rc = verifier.main(["--script", "x.js", "--notification", NOTIF, "--monitor", "A",
                        "--monitor", "B"], lister=fake_lister)
    out = json.loads(capsys.readouterr().out)
    for key, value in expect.items():
        assert out[key] == value, out
    assert rc == (0 if expect["ok"] else 1)


def test_verifier_reports_when_kuma_cannot_be_asked(capsys: pytest.CaptureFixture[str]) -> None:
    def failing_lister(script: str, username: str, container: str = "") -> dict:
        raise RuntimeError("kuma-admin.js exited 5: login refused: authIncorrectCreds")

    assert verifier.main(["--script", "x.js", "--notification", NOTIF, "--monitor", "A"],
                         lister=failing_lister) == 2
    assert "authIncorrectCreds" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# Defaults that encode decisions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_tls_certificate_is_generated_renewed_and_follows_the_host_address(tmp_path: Path) -> None:
    task = find_task(tasks("tls.yml"), "Generate or renew the Kuma TLS certificate")
    script = task["ansible.builtin.shell"]["cmd"]
    assert task["changed_when"] == "uptime_kuma_tls_cert.stdout == 'changed'"

    def run(lan_ip: str = "192.168.1.129", days: str = "825") -> str:
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "DIR": str(tmp_path), "LAN_IP": lan_ip,
                                   "DAYS": days}, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout.strip()

    def san() -> str:
        return subprocess.run(["openssl", "x509", "-in", str(tmp_path / "cert.pem"), "-noout",
                               "-ext", "subjectAltName"], capture_output=True, text=True,
                              check=True).stdout

    assert run() == "changed"
    assert (tmp_path / "key.pem").stat().st_mode & 0o777 == 0o600
    assert "IP Address:192.168.1.129" in san() and "IP Address:127.0.0.1" in san()
    first = (tmp_path / "cert.pem").read_bytes()

    assert run() == "unchanged"                       # idempotent
    assert (tmp_path / "cert.pem").read_bytes() == first

    assert run(lan_ip="192.168.1.200") == "changed"   # the host moved
    assert "IP Address:192.168.1.200" in san()

    # A certificate expiring within 30 days is renewed on the next run.
    (tmp_path / "cert.pem").unlink()
    assert run(lan_ip="192.168.1.200", days="10") == "changed"
    assert run(lan_ip="192.168.1.200") == "changed"


def test_autokuma_rc_pin_keeps_its_reason() -> None:
    # The release-candidate pin is deliberate; without the reason next to it
    # the next tidy-up reverts it to 2.0.0, which stops syncing after any
    # Kuma restart (AutoKuma#157).
    text = (ROLE / "defaults" / "main.yml").read_text()
    assert defaults()["uptime_kuma_autokuma_image"] == (
        "ghcr.io/bigboot/autokuma:2.1.0-rc.2"
        "@sha256:12ed0e5085feeac65db760c1a3e3d4fa3acea0e7dc4c0e601e2ea64d82485a45")
    for needle in ("AutoKuma#157", "AutoKuma#166",
                   "Revisit when AutoKuma ships a stable release containing the #157 fix"):
        assert needle in text


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


@pytest.mark.parametrize("url, ok", [
    (FAKE_WEBHOOK, True),
    ("https://discordapp.com/api/webhooks/100000000000000001/abcdefghijklmnopqrstuvwxyz", True),
    ("", False),
    ("http://discord.com/api/webhooks/100000000000000001/abcdefghijklmnopqrstuvwxyz", False),
    ("https://discordXcom/api/webhooks/100000000000000001/abcdefghijklmnopqrstuvwxyz", False),
    ("https://discord.com.evil.example/api/webhooks/100000000000000001/abcdefghijklmnopqrst", False),
    ("https://discord.com/api/webhooks/100000000000000001/abcdefghijklmnopqrstuvwxyz?wait=1", False),
])
def test_webhook_shape_check(url: str, ok: bool) -> None:
    task = find_task(tasks("discord.yml"), "Check the Discord webhook URL is a Discord webhook")
    [cond] = task["ansible.builtin.assert"]["that"]
    assert render_text("{{ " + cond + " }}", host_vars(uptime_kuma_discord_webhook_url=url)) is ok
    assert task["no_log"] is True


def test_docker_key_pin_is_a_full_fingerprint() -> None:
    assert re.match(r"^[0-9A-F]{40}$", defaults()["uptime_kuma_docker_apt_key_fingerprint"])
