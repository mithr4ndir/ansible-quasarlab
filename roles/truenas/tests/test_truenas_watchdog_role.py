"""The Ansible half of the UGREEN watchdog codification.

Two properties carry real consequences and are therefore asserted rather than
reviewed:

1. `initshutdownscript.create` is not idempotent. It appends without checking,
   so an unguarded task adds one POSTINIT entry per run. The hourly timer would
   accumulate them forever and every one would execute at boot.
2. Both persistence layers must survive. The systemd unit beats the ~161s
   hardware deadline; the POSTINIT entry survives the TrueNAS version upgrade
   that wipes /etc. Losing either silently halves the protection.
"""

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
UNIT = (ROLE / "templates" / "ugreen-wdt-disable.service.j2").read_text()


def task(name):
    for t in TASKS:
        if t.get("name") == name:
            return t
    raise AssertionError(f"no task named {name!r}")


def test_the_create_task_is_guarded_on_there_being_no_existing_entry():
    t = task("Register the watchdog disable as a TrueNAS POSTINIT command")
    conds = [str(c) for c in t["when"]]
    assert any("truenas_wdt_postinit | length == 0" in c for c in conds), (
        "initshutdownscript.create appends unconditionally; without this guard "
        f"every run adds a duplicate. when: {conds}"
    )


def test_duplicate_postinit_entries_are_a_hard_failure():
    t = task("Assert at most one POSTINIT entry runs the watchdog script")
    that = [str(x) for x in t["ansible.builtin.assert"]["that"]]
    assert any("length <= 1" in x for x in that)


def test_a_disabled_postinit_entry_is_re_enabled():
    """Present in the query, so no duplicate is created, and inert at boot.
    Without this task that state is invisible and permanent."""
    t = task("Re-enable the watchdog POSTINIT entry if it was switched off")
    conds = [str(c) for c in t["when"]]
    assert any("enabled" in c for c in conds)


def test_the_outcome_is_asserted_not_the_service_state():
    """`active` is satisfied by a unit that ran and cleared nothing. The script
    exits non-zero when the register does not read back 0, so only the exit
    status says the timer is actually disarmed."""
    t = task("Assert the watchdog timer was actually cleared")
    that = " ".join(str(x) for x in t["ansible.builtin.assert"]["that"])
    assert "ExecMainStatus" in str(task("Read the result of the watchdog disable"))
    assert "== '0'" in that


@pytest.mark.parametrize(
    "needle,why",
    [
        ("DefaultDependencies=no", "must not wait for the normal boot ordering"),
        ("WantedBy=sysinit.target", "must start early enough to beat ~161s"),
        ("Type=oneshot", "it is a one-shot register write"),
        ("RemainAfterExit=yes", "so a success is not re-run as a failure"),
        ("TimeoutStartSec=", "a hung boot-critical oneshot blocks everything after it"),
    ],
)
def test_the_unit_is_shaped_to_beat_the_hardware_deadline(needle, why):
    assert needle in UNIT, why


def test_both_persistence_layers_are_present():
    names = [t.get("name", "") for t in TASKS]
    assert "Deploy the UGREEN watchdog systemd unit" in names, "layer 1: beats 161s"
    assert "Register the watchdog disable as a TrueNAS POSTINIT command" in names, (
        "layer 2: survives the upgrade that wipes /etc"
    )


def test_the_role_installs_a_boot_mechanism_rather_than_writing_registers_itself():
    """If Ansible never runs again the NAS must still disarm its own watchdog.
    No task may perform the register write directly.

    Comments are stripped before checking: the tasks legitimately explain what
    the script does, and an earlier version of this test failed on its own
    prose rather than on a real task.
    """
    code = [
        ln for ln in (ROLE / "tasks" / "main.yml").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    offenders = [ln.strip() for ln in code if "/dev/port" in ln]
    assert offenders == [], offenders


def test_the_whole_thing_can_be_switched_off_by_one_flag():
    assert DEFAULTS["truenas_wdt_disable_enabled"] is True
    guarded = [
        t.get("name")
        for t in TASKS
        if "wdt" in str(t).lower()
        and "truenas_wdt_disable_enabled" not in str(t.get("when", ""))
    ]
    assert guarded == [], f"watchdog tasks not behind the flag: {guarded}"
