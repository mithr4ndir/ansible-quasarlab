"""The two ansible timers must not share SSH mux sockets or a wall-clock slot.

Both units run as the same user with the same HOME, so with Ansible's default
control_path_dir (~/.ansible/cp) they share one ControlMaster socket per target
host. They are Type=oneshot with KillMode=control-group, so when one finishes,
systemd SIGTERMs everything left in its cgroup and the lingering ControlPersist
masters die with it, tearing down the socket the OTHER service is mid-task on.
That surfaced as "Shared connection to <ip> closed" on rotating hosts: 17 of 17
such failures between 2026-09-21 and 2026-09-27 landed 0-6s after the other
service's last playbook ended. Reproduced directly: shared socket gives ssh
rc=255 with the remote task cut off, separate dirs give rc=0.

The timers also drifted, because OnUnitActiveSec counts from the last
activation, so each period was the interval PLUS run duration PLUS jitter.
The two drifted at different rates and periodically collided, which is what
made the failures bursty. One collision produced a dpkg frontend lock failure
on pve, and dpkg_selections has no lock_timeout to ride that out.
"""

from __future__ import annotations

import configparser
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "roles" / "cmd_center" / "templates"
SERVICES = {s: TEMPLATES / f"ansible-{s}.service.j2" for s in ("proxmox", "security")}
TIMERS = {s: TEMPLATES / f"ansible-{s}.timer.j2" for s in ("proxmox", "security")}


def directive(text: str, key: str) -> list[str]:
    """Values for a systemd directive, ignoring comment lines."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if line.startswith(f"{key}="):
            out.append(line.split("=", 1)[1].strip())
    return out


class ControlPathIsolationTests(unittest.TestCase):
    def test_each_service_declares_a_runtime_directory(self):
        for name, path in SERVICES.items():
            with self.subTest(service=name):
                got = directive(path.read_text(), "RuntimeDirectory")
                self.assertEqual(
                    len(got), 1, f"ansible-{name}.service needs exactly one RuntimeDirectory"
                )

    def test_each_service_points_ansible_at_its_own_control_dir(self):
        for name, path in SERVICES.items():
            with self.subTest(service=name):
                envs = directive(path.read_text(), "Environment")
                cp = [e for e in envs if e.startswith("ANSIBLE_SSH_CONTROL_PATH_DIR=")]
                self.assertEqual(
                    len(cp), 1,
                    f"ansible-{name}.service must set ANSIBLE_SSH_CONTROL_PATH_DIR, "
                    "or it falls back to the shared ~/.ansible/cp",
                )
                runtime_dir = directive(path.read_text(), "RuntimeDirectory")[0]
                self.assertEqual(
                    cp[0].split("=", 1)[1], f"/run/{runtime_dir}",
                    "control path dir must be the dir systemd actually creates",
                )

    def test_the_two_services_do_not_share_a_control_dir(self):
        """The whole point: a cgroup kill must only reap that service's masters."""
        dirs = {
            n: directive(p.read_text(), "ANSIBLE_SSH_CONTROL_PATH_DIR") or
               [e for e in directive(p.read_text(), "Environment")
                if e.startswith("ANSIBLE_SSH_CONTROL_PATH_DIR=")]
            for n, p in SERVICES.items()
        }
        vals = [v[0] for v in dirs.values()]
        self.assertEqual(len(set(vals)), 2, f"both services use the same control dir: {vals}")


class TimerScheduleTests(unittest.TestCase):
    def test_timers_use_fixed_slots_not_drifting_intervals(self):
        for name, path in TIMERS.items():
            with self.subTest(timer=name):
                text = path.read_text()
                self.assertTrue(
                    directive(text, "OnCalendar"),
                    f"ansible-{name}.timer must use OnCalendar",
                )
                self.assertFalse(
                    directive(text, "OnUnitActiveSec"),
                    f"ansible-{name}.timer still uses OnUnitActiveSec, which drifts "
                    "by the run duration every period and re-creates the collision",
                )

    def test_slots_do_not_coincide(self):
        minutes = {}
        for name, path in TIMERS.items():
            mins = set()
            for expr in directive(path.read_text(), "OnCalendar"):
                tail = expr.split(":")[1] if ":" in expr else ""
                mins |= {int(m) for m in re.findall(r"\d+", tail)}
            minutes[name] = mins
        self.assertTrue(minutes["proxmox"] and minutes["security"])
        self.assertFalse(
            minutes["proxmox"] & minutes["security"],
            f"timers share a start minute: {minutes}",
        )


class SshRetryTests(unittest.TestCase):
    """`retries` only exists under [ssh_connection]; under [defaults] it is inert."""

    def setUp(self):
        self.cfg = configparser.ConfigParser()
        self.cfg.read(REPO / "ansible.cfg")

    def test_ssh_retries_are_configured(self):
        self.assertTrue(
            self.cfg.has_option("ssh_connection", "retries"),
            "ansible.cfg has no [ssh_connection] retries, so a single reaped "
            "socket fails the whole run (ansible defaults to 0)",
        )
        self.assertGreaterEqual(self.cfg.getint("ssh_connection", "retries"), 1)

    def test_retries_is_not_misplaced_under_defaults(self):
        self.assertFalse(
            self.cfg.has_option("defaults", "retries"),
            "there is no `retries` ini key under [defaults]; it parses clean "
            "and does nothing, which is the vacuous-config trap",
        )
