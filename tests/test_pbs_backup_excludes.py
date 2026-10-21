"""The PVE backup job is `all 1`, so the exclusion lists ARE the policy.

Three things are pinned here, in increasing order of how quietly they break.

1. The exclusions that exist stay: both PBS guests, the one guest whose disk
   carries a vacuous `backup=0`, and template discovery.
2. Every excluded vmid is explained in a comment. An exclusion is a decision
   not to hold a copy of a guest, and an unexplained one cannot be reviewed or
   safely removed later.
3. Both lists are actually consumed by the task that computes the job's
   `--exclude`, and the result is sorted and deduplicated. A list nobody reads
   is the `host_vars/cmd_center1` failure mode: editing it looks like a change
   and does nothing. An unstable order makes the task report changed forever.

Written while chasing a daily backup alert that turned out to be vm122 sharing
a GPU with vm121 (see the hazard note in the role defaults). vm122 was deleted
instead of excluded, so no exclusion came of it, but the role had no test over
its exclusion policy at all and now does.
"""

import pathlib
import re

import pytest
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1] / "roles" / "pve" / "pbs_storage"
DEFAULTS_PATH = ROLE / "defaults" / "main.yml"
TASKS_PATH = ROLE / "tasks" / "main.yml"

DEFAULTS = yaml.safe_load(DEFAULTS_PATH.read_text())
DEFAULTS_TEXT = DEFAULTS_PATH.read_text()

STATIC_LISTS = ("pbs_backup_exclude_vmids", "pbs_backup_exclude_pbs_vmids")


def _excluded_vmids():
    out = []
    for name in STATIC_LISTS:
        out.extend(DEFAULTS[name])
    return out


def _computed_exclusion_expression():
    tasks = yaml.safe_load(TASKS_PATH.read_text())
    computed = [
        t
        for t in tasks
        if "pbs_backup_exclude_computed" in (t.get("ansible.builtin.set_fact") or {})
    ]
    assert len(computed) == 1, "expected exactly one task to compute the exclusion"
    return computed[0]["ansible.builtin.set_fact"]["pbs_backup_exclude_computed"]


def _comment_lines():
    return [ln for ln in DEFAULTS_TEXT.splitlines() if ln.lstrip().startswith("#")]


def test_pbs_guests_and_vacuous_backup_flag_guest_stay_excluded():
    # Regression guard on the three exclusions that predate vm122, so a later
    # edit to this list cannot drop one while adding another.
    assert set(DEFAULTS["pbs_backup_exclude_pbs_vmids"]) == {103, 120}
    assert 9000 in DEFAULTS["pbs_backup_exclude_vmids"]
    assert DEFAULTS["pbs_backup_exclude_templates"] is True


@pytest.mark.parametrize("vmid", _excluded_vmids())
def test_every_exclusion_is_explained_in_a_comment(vmid):
    # Not a style check. Each of these is a guest we have decided not to keep a
    # copy of, and the reason is what tells a future reader whether the
    # exclusion is still true.
    #
    # Parametrized rather than looped, so one undocumented vmid is reported by
    # name instead of the whole set failing on the first one. Deliberately NOT
    # the `subtests` fixture: that needs pytest 9 or pytest-subtests, and the
    # runner documented in AGENTS.md resolves pytest 8.4.1, where the fixture
    # is missing and the test ERRORS rather than running. Which is how this
    # file shipped in #244 with its two best assertions never executing.
    comments = "\n".join(_comment_lines())
    assert re.search(rf"\bvm{vmid}\b", comments), (
        f"vmid {vmid} is excluded from backups with no explanation in "
        f"{DEFAULTS_PATH.name}. Say why, and under what condition it "
        f"should be included again."
    )


def test_both_static_lists_feed_the_computed_exclusion():
    # The guard that makes the tests above mean anything. If the set_fact
    # that builds `--exclude` stops reading one of these lists, editing it is
    # inert and the job silently backs the guest up again.
    expr = _computed_exclusion_expression()
    for name in STATIC_LISTS:
        assert name in expr, f"{name} is not read by the exclusion computation"
    # And it must reach the job, sorted, as a comma-joined string.
    assert "sort" in expr and "join(',')" in expr


def test_computed_exclusion_renders_from_the_roles_own_expression():
    """Render the real set_fact expression, not a reimplementation of it.

    PVE stores `exclude` as a string and the role diffs that string to decide
    whether to call `pvesh set`, so the order and the deduplication are part of
    the contract: an unstable order makes the task report changed on every run.
    Asserting against a hand-written copy of the pipeline would pass even if
    the role's own expression lost `sort` or `unique`, so the expression comes
    out of tasks/main.yml and is rendered with Ansible's real filters.
    """
    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment

    expr = _computed_exclusion_expression()
    env = Environment()
    env.filters.update(FilterModule().filters())

    # Shaped like `pvesh get /cluster/resources --type vm`: the four templates
    # the lab actually has, one of them (9000) also in the static list so the
    # dedup is exercised, plus a running guest that must NOT be excluded.
    resources = (
        '[{"vmid": 102, "template": 1}, {"vmid": 9000, "template": 1},'
        ' {"vmid": 9001, "template": 1}, {"vmid": 9002, "template": 1},'
        ' {"vmid": 113, "template": 0}, {"vmid": 121}]'
    )
    context = dict(
        DEFAULTS,
        pbs_cluster_vms={"stdout": resources},
    )
    rendered = env.from_string(expr).render(**context).strip()

    assert rendered == "102,103,120,9000,9001,9002"

    fields = rendered.split(",")
    for vmid in _excluded_vmids():
        assert str(vmid) in fields, f"{vmid} never reaches the job's --exclude"
    assert "113" not in fields, "a running non-template guest was excluded"
    assert fields.count("9000") == 1, "9000 is listed twice and was not deduped"
