"""Every registered variable this role later dereferences must exist under
--check.

The defect (2026-09-19, found by the operator on the first real
`scripts/run-uptime-kuma.sh --check`): `ansible.builtin.tempfile` has no
check mode, so "Create private scratch directory for the Docker key
download" was skipped, its registered result had no `path`, and the next
task died with "'dict object' has no attribute 'path'".

No playbook is run (that is what missed this in the first place). Instead
the role's task files are walked in import order, blocks flattened and
`when` conditions inherited, and for every `register:` the consumers are
checked. A task whose module has no check mode, or that is itself skipped
under --check, may only be consumed by a task that is also skipped under
--check or that guards the reference with `default(` / `is defined`.

Ansible evaluates a `when` list item by item and stops at the first false
one, so a leading `not ansible_check_mode` is what makes a later reference
in the same task safe.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]

# Modules used by this role that do NOT support check mode: under --check
# they are skipped, so their registered result holds nothing useful.
NO_CHECK_MODE = {
    "ansible.builtin.tempfile",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.script",
    "ansible.builtin.raw",
    "ansible.builtin.uri",
}
MODULE_KEYS = re.compile(r"^(ansible\.builtin\.|community\.docker\.)")
TASK_KEYWORDS = {
    "name", "when", "register", "loop", "loop_control", "with_items", "become",
    "become_user", "changed_when", "failed_when", "no_log", "check_mode", "tags",
    "ignore_errors", "retries", "delay", "until", "environment", "delegate_to",
    "run_once", "notify", "vars", "args", "block", "rescue", "always", "listen",
}


def task_files() -> dict[str, list[dict]]:
    return {path.name: yaml.safe_load(path.read_text()) or []
            for path in sorted((ROLE / "tasks").glob("*.yml"))}


def module_of(task: dict) -> str:
    for key in task:
        if MODULE_KEYS.match(key):
            return key
    return next((k for k in task if k not in TASK_KEYWORDS), "")


def when_list(task: dict) -> list[str]:
    value = task.get("when", [])
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def flatten(tasks: list[dict], inherited: list[str]) -> Iterator[dict]:
    """Tasks in execution order, each with the conditions that gate it."""
    for task in tasks:
        conditions = inherited + when_list(task)
        if any(key in task for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from flatten(task.get(key) or [], conditions)
            continue
        yield {**task, "_when": conditions}


def role_tasks() -> list[dict]:
    """main.yml order, with the import-level `when` inherited, as Ansible does."""
    files = task_files()
    ordered: list[dict] = []
    for entry in files["main.yml"]:
        included = entry.get("ansible.builtin.import_tasks") or entry.get("import_tasks")
        assert included, f"main.yml has a task that is not an import: {entry.get('name')}"
        for task in flatten(files[included], when_list(entry)):
            ordered.append({**task, "_file": included})
    return ordered


def skipped_under_check(task: dict) -> bool:
    if task.get("check_mode") is False:
        return False
    if any("ansible_check_mode" in cond for cond in task["_when"]):
        return True
    return module_of(task) in NO_CHECK_MODE


def guarded_in_check_mode(task: dict) -> bool:
    return any(cond.strip() in ("not ansible_check_mode", "not ansible_check_mode | bool")
               for cond in task["_when"])


def texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if not str(key).startswith("_"):
                yield from texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)


def references(task: dict, name: str) -> list[str]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return [text for text in texts(task) if pattern.search(text)]


def reference_is_defended(text: str, name: str) -> bool:
    """The reference itself copes with the variable being absent."""
    return bool(re.search(rf"{re.escape(name)}[^|]*\|\s*default\(", text)
                or re.search(rf"{re.escape(name)}[\w.]*\s+is\s+(not\s+)?defined", text))


def producer_consumer_pairs() -> list[tuple[dict, dict, str]]:
    ordered = role_tasks()
    pairs = []
    for index, producer in enumerate(ordered):
        name = producer.get("register")
        if not name:
            continue
        for consumer in ordered[index + 1:]:
            for text in references(consumer, name):
                pairs.append((producer, consumer, text))
    return pairs


PAIRS = producer_consumer_pairs()


def test_the_sweep_actually_finds_registered_variables() -> None:
    # Guard against the walk silently finding nothing (a vacuous pass).
    assert len({p.get("register") for p, _, _ in PAIRS}) >= 5
    assert {t["_file"] for t in role_tasks()} >= {
        "docker.yml", "secrets.yml", "kuma.yml", "nfs_probe.yml", "verify.yml", "tls.yml"}


@pytest.mark.parametrize(
    "producer, consumer, text", PAIRS,
    ids=[f"{p['register']}:{c.get('name', '?')[:40]}" for p, c, _ in PAIRS])
def test_registered_variables_survive_check_mode(producer: dict, consumer: dict,
                                                 text: str) -> None:
    if not skipped_under_check(producer):
        return
    name = producer["register"]
    assert guarded_in_check_mode(consumer) or reference_is_defended(text, name), (
        f"{consumer.get('name')!r} in {consumer['_file']} uses {name}, which "
        f"{producer.get('name')!r} does not set under --check. Give the producer "
        f"check_mode: false, guard the consumer with `when: not ansible_check_mode`, "
        f"or default the reference.")
