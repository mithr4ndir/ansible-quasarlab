"""Behaviour tests for the pull-replication half of roles/pbs.

These render the role's real task arguments through Ansible's own Templar and
evaluate its real `when:` expressions through Ansible's Conditional, rather
than re-implementing either. A test that renders the templates with plain
Jinja would pass against filters Ansible does not have.

The property worth protecting is `remove-vanished`. With it true the replica
stops being a second copy and becomes a mirror: anything deleted on pbs1,
whether by a correct prune, a mistake, or an attacker, is deleted here on the
next sync. That propagates the exact failure the replica exists to survive, so
a change that flips it has to fail a test rather than merely look reasonable
in review.
"""

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
REPLICATION = yaml.safe_load((ROLE / "tasks" / "replication.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
PBS1 = yaml.safe_load((REPO / "host_vars" / "pbs1" / "vars.yml").read_text())
PBS2 = yaml.safe_load((REPO / "host_vars" / "pbs2" / "vars.yml").read_text())


def _task(name):
    for t in REPLICATION:
        if t.get("name") == name:
            return t
    raise AssertionError(f"no task named {name!r} in replication.yml")


def _render(value, variables):
    from ansible.parsing.dataloader import DataLoader
    from ansible.plugins.loader import init_plugin_loader
    from ansible.template import Templar

    init_plugin_loader()
    loader = DataLoader()
    return Templar(loader=loader, variables=variables).template(value)


def _when(expressions, variables):
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook.conditional import Conditional
    from ansible.plugins.loader import init_plugin_loader
    from ansible.template import Templar

    init_plugin_loader()
    loader = DataLoader()
    c = Conditional(loader=loader)
    c.when = expressions if isinstance(expressions, list) else [expressions]
    return c.evaluate_conditional(
        Templar(loader=loader, variables=variables), variables
    )


def _flags(argv):
    """Map --flag -> following value, for asserting on a rendered argv."""
    return {
        argv[i]: argv[i + 1]
        for i in range(len(argv) - 1)
        if str(argv[i]).startswith("--")
    }


def _job_vars(job, extra=None):
    v = dict(DEFAULTS, **PBS2)
    v["item"] = job
    v.update(extra or {})
    return v


# --------------------------------------------------------------- inertness


def test_replication_does_nothing_on_a_host_that_configures_neither_end():
    """A PBS host that is neither a source nor a replica runs none of this."""
    assert DEFAULTS["pbs_sync_readers"] == []
    assert DEFAULTS["pbs_sync_remotes"] == []
    assert DEFAULTS["pbs_sync_jobs"] == []

    include = next(
        t for t in MAIN if t.get("name") == "Configure pull replication"
    )
    assert not _when(include["when"], dict(DEFAULTS))


def test_replication_runs_on_both_configured_ends():
    include = next(
        t for t in MAIN if t.get("name") == "Configure pull replication"
    )
    assert _when(include["when"], dict(DEFAULTS, **PBS1)), "pbs1 is the source"
    assert _when(include["when"], dict(DEFAULTS, **PBS2)), "pbs2 is the replica"


# -------------------------------------------------- remove-vanished safety


def test_remove_vanished_is_false_by_default():
    assert DEFAULTS["pbs_sync_remove_vanished"] is False


def test_created_sync_job_does_not_remove_vanished_snapshots():
    job = PBS2["pbs_sync_jobs"][0]
    argv = _render(_task("Create the sync jobs")["ansible.builtin.command"]["argv"],
                   _job_vars(job))
    assert _flags(argv)["--remove-vanished"] == "false"


def test_converged_sync_job_does_not_remove_vanished_snapshots():
    """The update path matters as much as create: it runs on every drift."""
    job = PBS2["pbs_sync_jobs"][0]
    argv = _render(
        _task("Converge the sync job settings")["ansible.builtin.command"]["argv"],
        _job_vars(job, {"item": {"item": job}}),
    )
    assert _flags(argv)["--remove-vanished"] == "false"


def test_remove_vanished_can_still_be_set_deliberately():
    """Proves the two tests above distinguish false from true rather than
    passing because the flag renders empty or is absent."""
    job = dict(PBS2["pbs_sync_jobs"][0], remove_vanished=True)
    argv = _render(_task("Create the sync jobs")["ansible.builtin.command"]["argv"],
                   _job_vars(job))
    assert _flags(argv)["--remove-vanished"] == "true"


def test_resync_corrupt_is_on_so_a_bad_local_chunk_is_repaired():
    job = PBS2["pbs_sync_jobs"][0]
    argv = _render(_task("Create the sync jobs")["ansible.builtin.command"]["argv"],
                   _job_vars(job))
    assert _flags(argv)["--resync-corrupt"] == "true"


# ------------------------------------------------------------ credentials


def test_the_token_secret_never_reaches_a_log():
    """The remote's token is an argv element, so without no_log it lands in
    the play output, the ara database and /var/log/ansible-quasarlab."""
    assert _task("Create the replication remotes").get("no_log") is True


def test_no_secret_is_committed_in_host_vars():
    for text in (
        (REPO / "host_vars" / "pbs1" / "vars.yml").read_text(),
        (REPO / "host_vars" / "pbs2" / "vars.yml").read_text(),
    ):
        loaded = yaml.safe_load(text)
        assert "password" not in str(loaded).lower()
        assert "token_env" in text or "token_authid" in text


def test_the_replica_credential_is_read_only():
    """DatastoreReader only. A replica that could write to the primary would
    let a compromised copy damage the thing it is copying."""
    roles = DEFAULTS["pbs_sync_reader_roles"]
    assert roles == ["DatastoreReader"]
    for role in roles:
        assert not any(
            w in role for w in ("Backup", "Prune", "Modify", "Admin", "Power")
        )


# -------------------------------------------------------------- pve client


def test_pve_client_is_configured_by_default_and_skipped_on_the_replica():
    assert DEFAULTS["pbs_configure_pve_client"] is True
    assert PBS1["pbs_configure_pve_client"] is True
    assert PBS2["pbs_configure_pve_client"] is False

    block = next(
        t for t in MAIN
        if t.get("name") == "Configure the PVE backup client identity"
    )
    assert _when(block["when"], dict(DEFAULTS, **PBS1))
    assert not _when(block["when"], dict(DEFAULTS, **PBS2))


def test_the_pve_token_assertion_is_still_strict():
    """The replica is accommodated by skipping the block, not by weakening the
    end-state assertion that caught the ACL intersection bug."""
    block = next(
        t for t in MAIN
        if t.get("name") == "Configure the PVE backup client identity"
    )
    assertion = next(
        t for t in block["block"]
        if t.get("name") == "Assert the token actually resolves the privileges it needs"
    )
    that = assertion["ansible.builtin.assert"]["that"]
    assert "'Datastore.Backup' in pbs_token_privs.stdout" in that
    assert "'Datastore.Audit' in pbs_token_privs.stdout" in that


# ------------------------------------------------------------- correctness


def test_the_pinned_fingerprint_is_a_wellformed_sha256():
    fp = PBS2["pbs_sync_remotes"][0]["fingerprint"]
    octets = fp.split(":")
    assert len(octets) == 32, f"sha256 is 32 octets, got {len(octets)}"
    assert all(len(o) == 2 and int(o, 16) >= 0 for o in octets)


def test_the_replica_pulls_from_the_primary_into_its_own_datastore():
    job = PBS2["pbs_sync_jobs"][0]
    remote = PBS2["pbs_sync_remotes"][0]
    assert job["remote"] == remote["name"]
    assert job["remote_store"] == DEFAULTS["pbs_datastore_name"]
    assert job["store"] == DEFAULTS["pbs_datastore_name"]
    assert remote["auth_id"] == PBS1["pbs_sync_readers"][0]["token_authid"], (
        "the replica must authenticate as the token pbs1 actually grants"
    )


def test_every_replication_task_has_a_name():
    for i, t in enumerate(REPLICATION):
        assert t.get("name"), f"task {i} in replication.yml is unnamed"


@pytest.mark.parametrize(
    "schedule",
    [DEFAULTS["pbs_sync_schedule"]]
    + [j.get("schedule") for j in PBS2["pbs_sync_jobs"] if j.get("schedule")],
)
def test_sync_schedules_are_valid_systemd_calendar_events(schedule):
    """PBS reports a bad schedule only from inside the create call, after the
    job is half-configured. `daily 03:30` is not valid and cost a failed run
    to discover: the shorthands are complete expressions and take no time."""
    import shutil
    import subprocess

    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    res = subprocess.run(
        ["systemd-analyze", "calendar", schedule],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"{schedule!r} rejected: {res.stderr.strip()}"


# ----------------------------------------------------------- idempotence


def _converge_when():
    return _task("Converge the sync job settings")["when"]


@pytest.mark.parametrize(
    "live_rate,should_change",
    [
        ("41943040 B", False),   # what PBS actually returns: already correct
        (41943040, False),       # plain integer, in case PBS ever changes
        ("20971520 B", True),    # genuinely different, must reconcile
        (None, True),            # unset, must be applied
    ],
)
def test_rate_in_comparison_survives_the_units_pbs_actually_returns(
    live_rate, should_change
):
    """PBS returns rate-in as the string "41943040 B", not a number.

    A bare `| int` on that yields 0, so the comparison was permanently unequal
    and this task reported changed on every run. Verified against pbs2 on
    2026-10-15 by running the play twice and getting changed=1 both times. A
    converge task that always fires is indistinguishable from one that never
    works.
    """
    job = PBS2["pbs_sync_jobs"][0]
    live = {
        "schedule": job["schedule"],
        "remove-vanished": False,
        "resync-corrupt": True,
    }
    if live_rate is not None:
        live["rate-in"] = live_rate

    import json

    variables = dict(DEFAULTS, **PBS2)
    variables["item"] = {"item": job, "stdout": json.dumps(live), "skipped": False}
    variables["pbs_sync_job_live"] = live
    assert _when(_converge_when()[1:], variables) is should_change
