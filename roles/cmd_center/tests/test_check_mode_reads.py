"""Check-mode tests for read-only command tasks in the cmd_center role.

Run from the repo root:
    uv run --with pytest pytest roles/cmd_center/tests

The defect (#159): `Read installed herdr version` is an ansible.builtin.command
task, and command does not execute under --check. It reported `skipping`, its
registered stdout was empty, and the assert after it failed every --check run
with a message blaming a version and checksum disagreement that did not exist.

The rule pinned here: a command or shell task whose registered result is read
by an assert or a `when:` must either run under --check (check_mode: false,
changed_when: false), or it and every task reading its result must all be
skipped under --check (`not ansible_check_mode`). shell_env.yml's grep for
`op read` is the second shape on purpose: the replace before it has not really
run under --check, so reading ~/.bashrc then would fail for no reason.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1]
TASKS = ROLE / "tasks"
SYSTEM_PYTHON = "/usr/bin/python3"
COMMAND_MODULES = {
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.legacy.command",
    "ansible.legacy.shell",
    "command",
    "shell",
}
ASSERT_MODULES = {"ansible.builtin.assert", "ansible.legacy.assert", "assert"}
HERDR_READ = "Read installed herdr version"
HERDR_ASSERT = "Assert installed herdr version matches the pin"
HERDR_INSTALL = "Install pinned herdr binary"


def load_yaml(path: Path):
    """Parse YAML with PyYAML, falling back to the system python3's copy.

    Failing (not skipping) when no parser exists is deliberate: a skipped
    wiring test proves nothing.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(path.read_text())
    except ImportError:
        proc = subprocess.run(
            [
                SYSTEM_PYTHON,
                "-c",
                "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)


def flatten(tasks: list[dict], inherited_when: list[str]) -> list[dict]:
    """Tasks in execution order, with block `when:` folded into each child."""
    out = []
    for task in tasks or []:
        when = task.get("when", [])
        when = inherited_when + (when if isinstance(when, list) else [when])
        if "block" in task:
            for key in ("block", "rescue", "always"):
                out.extend(flatten(task.get(key, []), when))
        else:
            out.append({**task, "_when": [str(w) for w in when]})
    return out


def role_tasks() -> list[tuple[str, dict]]:
    return [
        (path.name, task)
        for path in sorted(TASKS.glob("*.yml"))
        for task in flatten(load_yaml(path), [])
    ]


def module_of(task: dict, modules: set[str]) -> str | None:
    return next((m for m in modules if m in task), None)


def reads_register(task: dict, register: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(register)}\b")
    sources = list(task["_when"])
    assert_module = module_of(task, ASSERT_MODULES)
    if assert_module:
        sources.append(json.dumps(task[assert_module].get("that", [])))
    return any(pattern.search(s) for s in sources)


def skipped_in_check_mode(task: dict) -> bool:
    return any(re.search(r"\bnot\s+ansible_check_mode\b", w) for w in task["_when"])


def command_reads_with_consumers() -> list[tuple[str, dict, list[dict]]]:
    tasks = role_tasks()
    found = []
    for filename, task in tasks:
        register = task.get("register")
        if not register or not module_of(task, COMMAND_MODULES):
            continue
        consumers = [t for _, t in tasks if t is not task and reads_register(t, register)]
        if consumers:
            found.append((filename, task, consumers))
    return found


def test_scan_is_not_vacuous() -> None:
    names = {task["name"] for _, task, _ in command_reads_with_consumers()}
    assert HERDR_READ in names
    assert "Look for any op read left in ~/.bashrc" in names


@pytest.mark.parametrize(
    "filename,task,consumers",
    command_reads_with_consumers(),
    ids=lambda v: v["name"] if isinstance(v, dict) else None,
)
def test_command_feeding_assert_or_when_is_check_mode_safe(filename, task, consumers) -> None:
    runs_in_check_mode = task.get("check_mode") is False
    if runs_in_check_mode:
        # Running under --check is only safe for a task that changes nothing.
        assert task.get("changed_when") is False, (
            f"{filename}: '{task['name']}' sets check_mode: false, so it must "
            "also set changed_when: false"
        )
        return
    both_skip = skipped_in_check_mode(task) and all(skipped_in_check_mode(c) for c in consumers)
    assert both_skip, (
        f"{filename}: '{task['name']}' is skipped under --check, but "
        f"{[c['name'] for c in consumers]} read its result. Set check_mode: false "
        "and changed_when: false on it, or skip it and every reader under --check."
    )


# --- herdr version read under --check -----------------------------------------
#
# check_mode: false alone would break a --check run on a host where the binary
# is missing, or drifted with cmd_center_herdr_upgrade=true: get_url does not
# download under --check, so the read would hit a missing or old binary. The
# read and the assert are skipped exactly when --check reports the install as
# pending. The `when` lists go through Ansible's own evaluator.

EVALUATE_WHEN = r"""
import json, sys
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.conditional import Conditional
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar
init_plugin_loader()
data = json.load(sys.stdin)
cond = Conditional(loader=DataLoader())
cond.when = data["when"]
print(json.dumps(cond.evaluate_conditional(Templar(loader=DataLoader()), data["vars"])))
"""


def _ansible_available() -> bool:
    return Path(SYSTEM_PYTHON).exists() and subprocess.run(
        [SYSTEM_PYTHON, "-c", "import ansible.playbook.conditional"],
        capture_output=True,
        check=False,
    ).returncode == 0


def herdr_task(name: str) -> dict:
    return next(t for t in flatten(load_yaml(TASKS / "herdr.yml"), []) if t["name"] == name)


def evaluate_when(when: list[str], variables: dict, tmp_path: Path) -> bool:
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    proc = subprocess.run(
        [SYSTEM_PYTHON, "-c", EVALUATE_WHEN],
        input=json.dumps({"when": when, "vars": variables}),
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        },
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# (check mode, registered install result, should the read and assert run)
INSTALL_CASES = [
    pytest.param(True, {"changed": False, "skipped": True, "skip_reason": "x"}, True,
                 id="check-binary-present-install-skipped"),
    pytest.param(True, {"changed": False}, True, id="check-install-ran-no-change"),
    pytest.param(True, {"changed": True}, False, id="check-install-pending"),
    pytest.param(False, {"changed": False, "skipped": True, "skip_reason": "x"}, True,
                 id="apply-binary-present"),
    pytest.param(False, {"changed": True}, True, id="apply-just-installed"),
]


@pytest.mark.skipif(not _ansible_available(), reason="system python has no ansible")
@pytest.mark.parametrize("check_mode,install_result,expect_run", INSTALL_CASES)
def test_herdr_version_read_and_assert_skip_only_when_install_is_pending(
    check_mode, install_result, expect_run, tmp_path
) -> None:
    install = herdr_task(HERDR_INSTALL)
    register = install.get("register")
    assert register, f"'{HERDR_INSTALL}' must register its result for the read to key on"
    variables = {"ansible_check_mode": check_mode, register: install_result}
    for name in (HERDR_READ, HERDR_ASSERT):
        task = herdr_task(name)
        assert evaluate_when(task["_when"], variables, tmp_path) is expect_run, name


def test_herdr_version_read_runs_under_check_mode() -> None:
    task = herdr_task(HERDR_READ)
    assert task.get("check_mode") is False
    assert task.get("changed_when") is False
    names = [t["name"] for t in flatten(load_yaml(TASKS / "herdr.yml"), [])]
    assert names.index(HERDR_INSTALL) < names.index(HERDR_READ) < names.index(HERDR_ASSERT)
