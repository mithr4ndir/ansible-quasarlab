"""PVE notifications must reach Alertmanager, and successful backups must not.

Until 2026-10-15 `/etc/pve/notifications.cfg` was hand-edited and outside this
repo: one matcher with no rules (`mode all`) sent every notification to a
sendmail target. The nodes have no relayhost and outbound port 25 is blocked,
so every hourly "backup successful" mail sat in postfix (121 deferred per
node) and a failed backup, fencing or replication error notified nobody
(ansible-quasarlab #224).

`roles/pve/notifications/templates/notifications.cfg.j2` now renders the whole
file. These tests pin the properties that matter:

- info (backup succeeded, package updates) is not forwarded;
- warning and error are, to a webhook posting to Alertmanager's v2 API;
- the built-in default matcher and sendmail target are overridden and
  disabled. Omitting them is not enough: PVE falls back to its built-in
  defaults, which match everything and mail root@pam;
- the webhook body, once PVE's handlebars expressions are filled in, is a
  valid Alertmanager v2 alert list that the Discord route can render.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "roles" / "pve" / "notifications"
TEMPLATE = ROLE / "templates" / "notifications.cfg.j2"
BODY = ROLE / "files" / "alertmanager-body.json.hbs"
DEFAULTS = ROLE / "defaults" / "main.yml"
PLAYBOOK = REPO / "playbooks" / "proxmox.yml"


def _lookup(kind: str, name: str) -> str:
    assert kind == "ansible.builtin.file"
    # The file lookup strips the trailing newline, like Ansible's does.
    return (ROLE / "files" / name).read_text().rstrip("\n")


def render(**overrides) -> str:
    variables = yaml.safe_load(DEFAULTS.read_text())
    variables.update(overrides)
    variables["ansible_managed"] = "Ansible managed"
    # trim_blocks matches ansible.builtin.template's default.
    env = Environment(keep_trailing_newline=True, trim_blocks=True, undefined=StrictUndefined)
    env.filters["b64encode"] = lambda s: base64.b64encode(s.encode()).decode()
    env.globals["lookup"] = _lookup
    return env.from_string(TEMPLATE.read_text()).render(**variables)


def parse_sections(text: str) -> dict[tuple[str, str], dict[str, list[str]]]:
    """Minimal reader for PVE's section config format."""
    sections: dict[tuple[str, str], dict[str, list[str]]] = {}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            assert current is not None, f"property outside a section: {line!r}"
            key, _, value = line.strip().partition(" ")
            current.setdefault(key, []).append(value)
            continue
        kind, _, name = line.partition(":")
        assert (kind.strip(), name.strip()) not in sections, f"duplicate section {line!r}"
        current = sections.setdefault((kind.strip(), name.strip()), {})
    return sections


def matchers_routing(sections, severity: str, fields: dict[str, str]) -> set[str]:
    """Which targets a notification reaches, following PVE's matcher rules."""
    targets: set[str] = set()
    for (kind, _name), props in sections.items():
        if kind != "matcher" or props.get("disable") == ["true"]:
            continue
        results = []
        for sev in props.get("match-severity", []):
            results.append(severity in sev.split(","))
        for rule in props.get("match-field", []):
            mode, _, expr = rule.partition(":")
            field, _, values = expr.partition("=")
            assert mode == "exact", "only exact field matches are modelled here"
            results.append(fields.get(field) in values.split(","))
        mode = props.get("mode", ["all"])[0]
        matched = all(results) if mode == "all" else any(results)
        if props.get("invert-match") == ["true"]:
            matched = not matched
        if matched:
            for t in props.get("target", []):
                if (("sendmail", t) in sections and sections[("sendmail", t)].get("disable") == ["true"]) or (
                    ("webhook", t) in sections and sections[("webhook", t)].get("disable") == ["true"]
                ):
                    continue
                targets.add(t)
    return targets


@pytest.fixture(scope="module")
def sections():
    return parse_sections(render())


@pytest.mark.parametrize(
    "severity,fields",
    [
        ("info", {"type": "vzdump", "hostname": "pve"}),  # hourly backup succeeded
        ("info", {"type": "package-updates", "hostname": "pve"}),
        ("notice", {"type": "vzdump", "hostname": "pve"}),
    ],
)
def test_routine_notifications_go_nowhere(sections, severity, fields):
    assert matchers_routing(sections, severity, fields) == set()


@pytest.mark.parametrize(
    "severity,fields",
    [
        ("error", {"type": "vzdump", "hostname": "pve"}),  # backup failed
        ("error", {"type": "fencing", "hostname": "pve2"}),
        ("error", {"type": "replication", "hostname": "pve"}),
        ("warning", {"type": "vzdump", "hostname": "pve"}),
        ("unknown", {"type": "system-mail", "hostname": "pve"}),
    ],
)
def test_failures_reach_alertmanager_and_nothing_is_mailed(sections, severity, fields):
    assert matchers_routing(sections, severity, fields) == {"alertmanager"}


def test_system_mail_can_be_switched_off():
    s = parse_sections(render(pve_notifications_forward_system_mail=False))
    assert matchers_routing(s, "unknown", {"type": "system-mail", "hostname": "pve"}) == set()


def test_no_comment_lines():
    # PVE's parser fails the whole file on a `#` line: "line 1 - syntax error
    # (expected header)", verified against Proxmox::RS::Notify on pve 8.4.21.
    for line in render().splitlines():
        assert not line.lstrip().startswith("#"), line


def test_builtin_entries_are_overridden_and_disabled(sections):
    # Absent entries revert to PVE's built-in defaults: match all, mail root.
    assert sections[("matcher", "default-matcher")]["disable"] == ["true"]
    assert sections[("sendmail", "mail-to-root")]["disable"] == ["true"]
    # The invalid from-address that produced ""@Quasar is gone.
    assert "from-address" not in sections[("sendmail", "mail-to-root")]


def test_webhook_posts_json_to_alertmanager_v2(sections):
    hook = sections[("webhook", "alertmanager")]
    assert hook["method"] == ["post"]
    assert hook["url"] == ["http://192.168.1.233:9093/api/v2/alerts"]
    headers = {}
    for h in hook["header"]:
        props = dict(p.split("=", 1) for p in h.split(","))
        headers[props["name"]] = base64.b64decode(props["value"]).decode()
    assert headers == {"Content-Type": "application/json"}
    # The body is stored base64-encoded, exactly the bytes of the .hbs file.
    assert base64.b64decode(hook["body"][0]).decode() == BODY.read_text().rstrip("\n")


def fill_handlebars(body: str, severity: str, title: str, message: str, fields: dict[str, str]) -> str:
    """Stand-in for PVE's renderer, covering only the constructs the body uses."""

    def block(m):
        return "critical" if severity == m.group(1) else "warning"

    out = re.sub(r'\{\{#if \(eq severity "(\w+)"\)\}\}critical\{\{else\}\}warning\{\{/if\}\}', block, body)

    def esc(s: str) -> str:
        return json.dumps(s)[1:-1]

    def expr(m):
        e = m.group(1).strip()
        if e == "severity":
            return severity
        if e == "escape title":
            return esc(title)
        if e == "escape message":
            return esc(message)
        if e.startswith("fields."):
            name = e[len("fields."):].strip("[]")
            return fields.get(name, "")
        raise AssertionError(f"unmodelled handlebars expression {{{{{e}}}}}")

    out = re.sub(r"\{\{([^#/}][^}]*)\}\}", expr, out)
    assert "{{" not in out, out
    return out


@pytest.mark.parametrize("severity,expected", [("error", "critical"), ("warning", "warning"), ("unknown", "warning")])
def test_body_is_a_valid_alertmanager_alert(severity, expected):
    rendered = fill_handlebars(
        BODY.read_text(),
        severity=severity,
        title='vzdump backup status (pve.home.arpa): backup failed',
        message='Details\n"quoted"\tand tabbed\n',
        fields={"type": "vzdump", "hostname": "pve", "job-id": "99a51b34"},
    )
    alerts = json.loads(rendered)
    assert isinstance(alerts, list) and len(alerts) == 1
    alert = alerts[0]
    assert set(alert) == {"labels", "annotations"}
    labels = alert["labels"]
    assert labels["alertname"] == "PveNotification"
    assert labels["source"] == "proxmox"
    # The lab's routes, inhibit rules and Discord colours key on critical/warning.
    assert labels["severity"] == expected
    assert labels["pve_severity"] == severity
    assert labels["instance"] == "pve"
    assert labels["job_id"] == "99a51b34"
    for name in labels:
        assert re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name), name
    assert alert["annotations"]["summary"].endswith("backup failed")
    assert '"quoted"' in alert["annotations"]["description"]


def test_role_runs_from_the_hourly_proxmox_playbook():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    roles = [r if isinstance(r, str) else r.get("role") for p in plays for r in p.get("roles", [])]
    assert "pve/notifications" in roles


def test_written_once_per_run_with_pmxcfs_ownership():
    priv, public = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    for task in (priv, public):
        assert task["run_once"] is True
    t = public["ansible.builtin.template"]
    assert (t["dest"], t["owner"], t["group"], t["mode"]) == ("/etc/pve/notifications.cfg", "root", "www-data", "0640")
    c = priv["ansible.builtin.copy"]
    assert (c["dest"], c["owner"], c["group"], c["mode"]) == ("/etc/pve/priv/notifications.cfg", "root", "www-data", "0600")


def test_every_webhook_has_a_private_section(sections):
    # PVE refuses to instantiate a webhook with no private section ("private
    # config does not exist"), verified on pve 8.4.21, and then delivers nothing.
    priv = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())[0]
    private = parse_sections(priv["ansible.builtin.copy"]["content"])
    for kind, name in sections:
        if kind == "webhook":
            assert (kind, name) in private, f"webhook {name} has no private section"
