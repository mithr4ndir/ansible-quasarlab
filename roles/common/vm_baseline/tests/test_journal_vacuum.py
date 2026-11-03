"""The journal vacuum must be quiet in the steady state and loud when it matters.

This task ran unconditionally until 2026-11-03 and reported `changed` on one or
two hosts every hour, forever, because journald enforces `SystemMaxUse` only
when it rotates and therefore leaves the journal up to one `SystemMaxFileSize`
over the cap as its normal resting state. Every one of those was an honest
`changed`, which is precisely why it was a problem: it kept
`AnsiblePlaybookMadeChanges{playbook="vm_baseline.yml"}` lit permanently, and an
alert that is always on carries no information.

Two failure directions are pinned here, because the fix sits between them:

1. It must NOT run for routine overshoot. 33.7M over a 500M cap with a 50M
   rotation granularity is journald working as designed. If this regresses, the
   alert goes back to being permanently on and nobody notices the next real
   change.
2. It must still run when the journal is genuinely over budget. The task exists
   for the case the cap is not being honoured, and a guard that never fires
   would quietly remove the protection while looking like a tidy-up.

The unit arithmetic gets its own tests because `journalctl --disk-usage` picks
its suffix from the size. Reading "8.0K" as 8 megabytes, or "1.2G" as 1.2, moves
the threshold by three orders of magnitude and would break one of the two
directions above without touching the other.

The expressions are read out of tasks/main.yml and rendered with Ansible's own
filters rather than reimplemented, so losing the guard in the role fails these
tests instead of leaving a hand-written copy passing.
"""

import pathlib
import re

import pytest
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

BUDGET_TASK = "Work out the journal size budget in bytes"
VACUUM_TASK = "Vacuum the journal when it is further over the cap than one journal file"
USAGE_TASK = "Measure how much space the journal is using"

MIB = 1024 ** 2
GIB = 1024 ** 3


def _env():
    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment

    env = Environment()
    env.filters.update(FilterModule().filters())
    return env


def _task(name):
    found = [t for t in TASKS if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, got {len(found)}"
    return found[0]


def _budget(usage_stdout, cap=None, slack=None):
    """Render the role's real set_fact expressions against journalctl output."""
    env = _env()
    task = _task(BUDGET_TASK)
    ctx = dict(task["vars"])
    ctx["vm_baseline_journal_usage"] = {"stdout": usage_stdout}
    ctx["vm_baseline_journald_max_use"] = cap or DEFAULTS["vm_baseline_journald_max_use"]
    ctx["vm_baseline_journald_max_file_size"] = (
        slack or DEFAULTS["vm_baseline_journald_max_file_size"]
    )

    # The task's own vars are themselves templates and resolve against each other.
    resolved = {}
    for key, value in ctx.items():
        if isinstance(value, str) and "{{" in value:
            resolved[key] = env.from_string(value).render(**ctx)
        else:
            resolved[key] = value

    out = {}
    for fact, expr in task["ansible.builtin.set_fact"].items():
        out[fact] = int(env.from_string(expr).render(**resolved))
    return out


def _would_vacuum(usage_stdout, cap=None, slack=None):
    """Render the role's real `when:` against a rendered budget."""
    env = _env()
    facts = _budget(usage_stdout, cap, slack)
    expr = _task(VACUUM_TASK)["when"]
    rendered = env.from_string("{{ (" + expr.strip() + ") | bool }}").render(**facts)
    return rendered == "True"


def _usage(text):
    return f"Archived and active journals take up {text} in the file system."


# --- the two failure directions ---------------------------------------------

def test_routine_overshoot_does_not_vacuum():
    """The exact regression: 33.7M over a 500M cap is journald's designed state.

    uptime-kuma at 2026-11-03 03:17Z freed 33.7M and reported `changed`. With a
    500M cap and 50M rotation granularity that is inside the slack, so there is
    nothing to correct and journald collects it at the next rotation.
    """
    assert _would_vacuum(_usage("533.7M")) is False


def test_a_journal_genuinely_over_budget_does_vacuum():
    # 500M cap + 50M slack = 550M. 600M is past it, so the cap is not holding.
    assert _would_vacuum(_usage("600M")) is True


# --- the boundary, from both sides ------------------------------------------

@pytest.mark.parametrize(
    "used, expected",
    [
        ("480.9M", False),   # under the cap entirely, the measured local state
        ("500M", False),     # exactly at the cap
        ("549M", False),     # inside the slack
        ("551M", True),      # one mebibyte past cap + slack
        ("1.2G", True),      # a real blowout
    ],
)
def test_the_threshold_sits_at_cap_plus_one_journal_file(used, expected):
    assert _would_vacuum(_usage(used)) is expected


def test_the_boundary_is_not_off_by_a_whole_file():
    """A case must sit on each side of the line, or the comparison proves nothing.

    550M is cap + slack exactly and must not vacuum; 551M must. Without both,
    swapping `>` for `>=`, or dropping the slack term, would keep every other
    test here green.
    """
    assert _would_vacuum(_usage("550M")) is False
    assert _would_vacuum(_usage("551M")) is True


# --- unit arithmetic --------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_bytes",
    [
        ("8.0K", 8 * 1024),
        ("480.9M", int(480.9 * MIB)),
        ("1.2G", int(1.2 * GIB)),
        ("512", 512),
    ],
)
def test_sizes_parse_as_1024_based_with_the_suffix_journalctl_chose(text, expected_bytes):
    assert _budget(_usage(text))["vm_baseline_journal_used_bytes"] == expected_bytes


def test_a_tiny_journal_is_not_read_as_megabytes():
    # 8.0K misread as 8M is still far under the cap, so it would not change the
    # outcome; 8.0K misread as 8G would vacuum a nearly empty journal forever.
    assert _budget(_usage("8.0K"))["vm_baseline_journal_used_bytes"] < MIB
    assert _would_vacuum(_usage("8.0K")) is False


def test_the_cap_and_slack_come_from_the_defaults_not_a_literal():
    facts = _budget(_usage("100M"))
    assert facts["vm_baseline_journal_cap_bytes"] == 500 * MIB
    assert facts["vm_baseline_journal_slack_bytes"] == 50 * MIB


def test_a_larger_cap_moves_the_threshold_with_it():
    # Proves the comparison reads the variables rather than hardcoding 550M.
    assert _would_vacuum(_usage("600M"), cap="1G", slack="50M") is False
    assert _would_vacuum(_usage("1200M"), cap="1G", slack="50M") is True


# --- shape guards -----------------------------------------------------------

def test_the_measure_task_still_reads_under_check_mode():
    # A measurement that honours check mode returns no stdout, and the budget
    # task then fails to parse rather than skipping, so this is load-bearing.
    task = _task(USAGE_TASK)
    assert task["changed_when"] is False
    assert task["check_mode"] is False


def test_the_vacuum_is_still_guarded():
    """The whole point. An unguarded vacuum is the bug this file exists for."""
    task = _task(VACUUM_TASK)
    assert "when" in task, "the vacuum must stay conditional"
    cond = task["when"]
    assert "vm_baseline_journal_used_bytes" in cond
    assert "vm_baseline_journal_cap_bytes" in cond
    assert "vm_baseline_journal_slack_bytes" in cond


def test_changed_when_still_ignores_the_freed_0B_lines():
    # /run/log/journal essentially always reports "freed 0B", so a plain
    # substring test would report unchanged even after freeing gigabytes.
    cond = _task(VACUUM_TASK)["changed_when"]
    assert "freed (?!0B)" in cond, "the negative lookahead is what makes this honest"


def test_no_task_vacuums_the_journal_unconditionally():
    """Generalised, so a second copy of this task cannot reintroduce the bug."""
    offenders = []
    for task in TASKS:
        spec = task.get("ansible.builtin.command")
        cmd = spec if isinstance(spec, str) else (spec or {}).get("cmd", "")
        argv = " ".join((spec or {}).get("argv", [])) if isinstance(spec, dict) else ""
        if "--vacuum-size" in f"{cmd} {argv}" and "when" not in task:
            offenders.append(task.get("name"))
    assert offenders == [], (
        f"these tasks vacuum the journal on every run and will light "
        f"AnsiblePlaybookMadeChanges permanently: {offenders}"
    )


# --- the silent-failure direction -------------------------------------------

ASSERT_TASK = "Fail if the journal usage line cannot be parsed"


def test_an_unparseable_usage_line_is_caught_rather_than_read_as_zero():
    """Jinja's `float` returns 0.0 for junk instead of raising.

    So a journalctl output format this regex does not match would make the
    vacuum's `when:` permanently false, silently stop enforcing the cap, and
    report a clean recap on every run. That is the same shape as the
    scrape-allowlist outages: the rule is not wrong, it is vacuous. The assert
    fails the run instead, and this pins that it is checking the usage line and
    not something incidental.
    """
    env = _env()
    cond = _task(ASSERT_TASK)["ansible.builtin.assert"]["that"]
    assert len(cond) == 1
    expr = cond[0]

    from ansible.plugins.test.core import TestModule

    env.tests.update(TestModule().tests())

    def holds(stdout):
        rendered = env.from_string("{{ (" + expr + ") | bool }}").render(
            vm_baseline_journal_usage={"stdout": stdout}
        )
        return rendered == "True"

    assert holds(_usage("480.9M")) is True
    assert holds(_usage("0B")) is True, "an empty journal still reports a number"
    assert holds("") is False
    assert holds("Journale belegen 480.9M im Dateisystem.") is False, (
        "a localized or reworded output must fail loudly, not parse as zero"
    )


def test_zero_bytes_would_not_vacuum_but_is_not_treated_as_a_parse_failure():
    # Distinguishes "legitimately empty" from "could not read it", which is the
    # distinction the assert exists to make.
    assert _budget(_usage("0B"))["vm_baseline_journal_used_bytes"] == 0
    assert _would_vacuum(_usage("0B")) is False
