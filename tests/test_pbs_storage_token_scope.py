"""The PBS storage role must not demand a credential it is not going to use.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest tests/test_pbs_storage_token_scope.py -rs

roles/pve/pbs_storage spends the `pve@pbs!pve` token secret in exactly two
tasks, and both are conditional: `pvesm add` when the storage is not registered
yet, and `pvesm set` when the credentials, fingerprint or content type have
drifted. A steady-state reconcile run, which is what changing the backup job's
schedule is, touches neither.

The role used to assert the secret unconditionally in its first task. The guard
was right and its position was wrong: the only place to get that secret is a
1Password read, and this lab has exhausted that metered quota four times
(project_2026-05-02, 2026-08-24, 2026-09-13). An operator with a one-line
schedule change to apply had to spend a vault read to do it, which is how a
quota gets exhausted by work that never needed the secret.

So these tests pin the shape rather than the wording: no unconditional use, a
guard for every use, each guard carrying the SAME condition as the task it
guards and running before it. A guard whose condition has drifted from its
task's is worse than none, because it reports a missing secret on a run that
would not have used it, or passes a run that will.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "roles" / "pve" / "pbs_storage" / "tasks" / "main.yml"

TOKEN_LOOKUP = "lookup('env', pbs_storage_token_env)"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _contains(value, needle: str) -> bool:
    """Recursive substring search. yaml.safe_dump is not usable here: it
    re-quotes the lookup as '{{ lookup(''env'', ...) }}' and the doubled quotes
    make the needle vanish, which is exactly how this test first passed for no
    reason at all."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_contains(v, needle) for v in value)
    return False


def _uses_token(task: dict) -> bool:
    """True when this task interpolates the secret into something it runs."""
    return any(
        _contains(value, TOKEN_LOOKUP)
        for key, value in task.items()
        if key not in {"name", "when", "ansible.builtin.assert"}
    )


def _is_token_guard(task: dict) -> bool:
    block = task.get("ansible.builtin.assert")
    if not block:
        return False
    return any(TOKEN_LOOKUP in str(clause) for clause in block.get("that", []))


def test_the_secret_is_still_guarded_somewhere():
    """Deleting the guards entirely must not look like a pass."""
    tasks = _tasks()
    assert [t for t in tasks if _is_token_guard(t)], (
        "no task asserts the PBS token secret is present. An unset variable "
        "would reach `pvesm add --password ''` and register a storage that "
        "authenticates against nothing."
    )


def test_no_guard_is_unconditional():
    """The defect this change fixed, stated as a test.

    An unconditional assert is not a wrong guard, it is a guard in the wrong
    place: it turns a reconcile run into a vault read. Nothing else here
    notices it, because an assert task does not itself spend the secret.
    """
    for position, task in enumerate(_tasks()):
        if not _is_token_guard(task):
            continue
        assert task.get("when"), (
            f"task {position} {task['name']!r} asserts the token secret with no "
            "`when`, so the role demands it on every run including ones that "
            "will not use it."
        )


def test_no_task_spends_the_secret_unconditionally():
    for position, task in enumerate(_tasks()):
        if not _uses_token(task):
            continue
        assert task.get("when"), (
            f"task {position} {task['name']!r} interpolates the token secret "
            "with no `when`, so every run of this role needs a 1Password read "
            "whether or not it is going to push a credential."
        )


def test_every_use_of_the_secret_is_guarded_by_the_same_condition():
    tasks = _tasks()
    guards = {str(t["when"]): i for i, t in enumerate(tasks) if _is_token_guard(t)}
    uses = [(i, t) for i, t in enumerate(tasks) if _uses_token(t)]
    assert uses, "no task uses the token secret; this role has changed shape"

    for position, task in uses:
        condition = str(task["when"])
        assert condition in guards, (
            f"task {position} {task['name']!r} runs under `when: {condition}` "
            f"and no assert guards exactly that condition. Guards present: "
            f"{sorted(guards)}. A guard with a different condition either "
            "blocks a run that needed no secret or waves through one that does."
        )
        assert guards[condition] < position, (
            f"the guard for {task['name']!r} is at task {guards[condition]}, "
            f"after the task at {position} that it guards. An assert that runs "
            "afterwards is decoration."
        )


@pytest.mark.parametrize(
    "condition",
    [
        "pbs_storage_id not in ((pbs_pvesm.stdout | from_json) | map(attribute='storage') | list)",
        "pbs_storage_needs_secret | bool",
    ],
)
def test_both_known_credential_paths_are_covered(condition):
    """Named explicitly, so dropping one path fails here rather than silently."""
    guards = {str(t["when"]) for t in _tasks() if _is_token_guard(t)}
    assert condition in guards, (
        f"no token guard for {condition!r}. The two paths that spend the secret "
        "are registering a storage that does not exist yet and pushing drifted "
        "credentials; both need the guard."
    )
