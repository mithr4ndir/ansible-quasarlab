"""Regression tests for how the role talks to the ansible_user systemd manager.

Run from the repo root:
    uv run --with pytest pytest roles/cmd_center/tests

The defect: playbooks/cmd_center.yml sets become at play level, so facts are
gathered as root and ansible_user_uid is 0. Three user-scope systemd tasks
built XDG_RUNTIME_DIR from that fact, pointed systemctl --user at the
nonexistent /run/user/0, and aborted the play with "Failed to connect to bus".
The `| default(1000)` never fired because the fact was defined, just wrong.

The chosen pattern, already proven by herdr.yml and ce_review_viewer.yml, is
`become: false` with no environment override: the task runs as the SSH
connection user, whose pam_systemd session already exports the right
XDG_RUNTIME_DIR. Nothing here runs Ansible.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
PLAYBOOK = REPO / "playbooks" / "cmd_center.yml"

SYSTEMD_MODULES = {
    "ansible.builtin.systemd",
    "ansible.builtin.systemd_service",
    "systemd",
    "systemd_service",
}
SHELL_MODULES = {
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "command",
    "shell",
}
BLOCK_KEYS = ("block", "rescue", "always")
SESSION_ENV = {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}

# Every user-scope unit task that exists today. Pinned so the consistency
# test cannot pass vacuously by finding nothing.
EXPECTED_USER_SCOPE_TASKS = {
    ("handlers/main.yml", "Reload user systemd"),
    ("handlers/main.yml", "Restart spec-workflow-dashboard"),
    ("tasks/spec_workflow.yml", "Enable and start spec-workflow-dashboard"),
    ("tasks/ce_review_viewer.yml", "Enable and start ce-review-viewer"),
    ("tasks/herdr.yml", "Enable herdr user service"),
    ("tasks/herdr.yml", "Enable and start herdr health collector timer"),
}


def load_yaml(path: Path) -> Any:
    """Parse YAML with PyYAML, falling back to the system python3's copy.

    The uv test env only guarantees pytest. Failing (not skipping) when no
    parser exists is deliberate: a skipped wiring test proves nothing.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(path.read_text())
    except ImportError:
        import json

        proc = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)


def role_task_files() -> list[Path]:
    return sorted((ROLE / "tasks").glob("*.yml")) + sorted((ROLE / "handlers").glob("*.yml"))


def walk_tasks(tasks: Any, inherited: dict[str, Any]) -> Iterator[tuple[dict, dict[str, Any]]]:
    """Yield every leaf task with the become/environment it inherits from blocks."""
    for task in tasks or []:
        scope = dict(inherited)
        for key in ("become", "become_user", "environment"):
            if key in task:
                scope[key] = task[key]
        if any(k in task for k in BLOCK_KEYS):
            for k in BLOCK_KEYS:
                yield from walk_tasks(task.get(k), scope)
        else:
            yield task, scope


def all_role_tasks() -> Iterator[tuple[str, dict, dict[str, Any]]]:
    for path in role_task_files():
        rel = path.relative_to(ROLE).as_posix()
        for task, scope in walk_tasks(load_yaml(path), {}):
            yield rel, task, scope


def is_user_scope_systemd(task: dict) -> bool:
    for module in SYSTEMD_MODULES & task.keys():
        args = task[module] or {}
        if isinstance(args, dict) and args.get("scope") == "user":
            return True
    return False


def is_shell_systemctl_user(task: dict) -> bool:
    for module in SHELL_MODULES & task.keys():
        if re.search(r"systemctl\s+--user", str(task[module])):
            return True
    return False


def test_play_runs_with_become_so_facts_describe_root() -> None:
    """The premise of the bug. If this ever changes, revisit the tests below."""
    plays = load_yaml(PLAYBOOK)
    play = next(p for p in plays if "cmd_center" in (p.get("roles") or []))
    assert play.get("become") is True


def test_ansible_user_uid_is_never_used_anywhere() -> None:
    """Under play-level become the fact is root's uid, not ansible_user's."""
    offenders = []
    for top in ("roles", "playbooks", "inventory", "group_vars", "host_vars"):
        base = REPO / top
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2"}:
                for lineno, line in enumerate(path.read_text().splitlines(), 1):
                    if "ansible_user_uid" in line and not line.lstrip().startswith("#"):
                        offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert offenders == []


def test_no_role_task_builds_a_user_runtime_path() -> None:
    offenders = []
    for path in role_task_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "/run/user" in line or any(var in line for var in SESSION_ENV):
                offenders.append(f"{path.relative_to(ROLE)}:{lineno}: {line.strip()}")
    assert offenders == []


def test_user_scope_task_inventory_is_as_expected() -> None:
    found = {(rel, task.get("name")) for rel, task, _ in all_role_tasks() if is_user_scope_systemd(task)}
    assert found == EXPECTED_USER_SCOPE_TASKS


@pytest.mark.parametrize("rel,name", sorted(EXPECTED_USER_SCOPE_TASKS))
def test_user_scope_task_runs_as_connection_user(rel: str, name: str) -> None:
    rel_task = next(
        (task, scope)
        for r, task, scope in all_role_tasks()
        if r == rel and task.get("name") == name
    )
    task, scope = rel_task
    # Explicit on the task itself: the play sets become, so omitting it
    # silently inherits root.
    assert task.get("become") is False, f"{rel}: {name} must set become: false"
    assert "become_user" not in scope, f"{rel}: {name} must not set become_user"
    env = scope.get("environment") or {}
    assert not (SESSION_ENV & set(env)), f"{rel}: {name} must not override the session env"


def test_every_user_scope_systemd_task_uses_the_same_pattern() -> None:
    """Catches new tasks, including systemctl --user through command or shell."""
    bad = []
    for rel, task, scope in all_role_tasks():
        if not (is_user_scope_systemd(task) or is_shell_systemctl_user(task)):
            continue
        env = scope.get("environment") or {}
        if task.get("become") is not False or "become_user" in scope or SESSION_ENV & set(env):
            bad.append(f"{rel}: {task.get('name')}")
    assert bad == []
