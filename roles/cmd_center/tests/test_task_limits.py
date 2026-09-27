"""Regression tests for tasks/task_limits.yml and the herdr unit's ceilings.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest roles/cmd_center/tests

Background: on 2026-09-21 herdr.service died with
'failed to spawn thread: Os { code: 11, kind: WouldBlock }' at TasksMax=1027,
systemd's DefaultTasksMax=15% resolved against a kernel.threads-max that
Proxmox memory hotplug had left sized for 1 GiB of RAM. The ceilings are
layered and the lowest binds, which a thread probe showed directly:

    TasksMax=1027, slice 2259  ->  died at 1026 threads
    TasksMax=4096, slice 2259  ->  died at 1969 threads
    TasksMax=4096, slice 8192  ->  died at 2971 threads (RLIMIT_NPROC 3424)

So these tests are not only 'the directive is present'. They evaluate the
ordering expressions the play itself asserts, against the role's real defaults,
so raising one ceiling without the ones outside it fails here.
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

import jinja2
import yaml


ROLE = Path(__file__).resolve().parent.parent
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "task_limits.yml"
MAIN = ROLE / "tasks" / "main.yml"
TEMPLATES = ROLE / "templates"

# command-center1: MemTotal 16644424 kB, which Ansible reports as 16254 MB.
MEMTOTAL_MB = 16254
# The ceiling herdr.service actually had when it crashed.
CRASH_TASKS_MAX = 1027
ASSERT_TASK = "Assert the task ceilings are ordered lowest to highest"
LIVE_ASSERT_TASK = "Assert the kernel ceiling in force can deliver those limits"
TARGET_TASK = (
    "Choose the ceiling to persist, never lower than computed, live or persisted"
)
SET_FACT_TASK = "Resolve the kernel thread ceiling the other limits have to fit under"


def _render(expr: str, variables: dict) -> str:
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(expr).render(**variables)


def _resolved_defaults() -> dict:
    """Role defaults plus the fact the play sets, resolved as a play would."""
    raw = yaml.safe_load(DEFAULTS.read_text())
    variables = {"ansible_memtotal_mb": MEMTOTAL_MB, "ansible_user": "ladino"}
    for key, value in raw.items():
        if not (isinstance(value, str) and "{{" in value):
            variables[key] = value
    for key, value in raw.items():
        if isinstance(value, str) and "{{" in value:
            variables[key] = _render(value, variables)
    # The thread ceiling is a set_fact in the task file, not a default, because
    # it reads a fact. Resolve it from the task so the test cannot drift from
    # the expression the play actually evaluates.
    fact = _task(TASKS, SET_FACT_TASK)["ansible.builtin.set_fact"]
    for key, value in fact.items():
        variables[key] = _render(value, variables)
    # The play reads the enforced ceiling from /proc and asserts against it too.
    # A real host that has had vm_baseline applied reports the computed value,
    # so that is what the happy path here supplies.
    variables["cmd_center_kernel_threads_max_live"] = variables[
        "cmd_center_kernel_threads_max"
    ]
    return variables


def _task(path: Path, name: str) -> dict:
    for task in yaml.safe_load(path.read_text()):
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {path}")


class CeilingOrderingTest(unittest.TestCase):
    """The play's own ordering expressions, evaluated against the defaults."""

    def setUp(self) -> None:
        self.variables = _resolved_defaults()

    def test_play_assertions_hold_for_the_shipped_defaults(self) -> None:
        conditions = _task(TASKS, ASSERT_TASK)["ansible.builtin.assert"]["that"]
        self.assertGreaterEqual(len(conditions), 3, "ordering assert lost conditions")
        for condition in conditions:
            rendered = _render("{{ " + condition + " }}", self.variables)
            self.assertEqual(
                rendered,
                "True",
                f"default values violate the play's own assertion: {condition}",
            )

    def test_kernel_ceiling_matches_the_kernel_formula(self) -> None:
        # mempages / 32 == MemTotal_kB / 128. 16644424 / 128 == 130034.
        self.assertAlmostEqual(
            int(self.variables["cmd_center_kernel_threads_max"]),
            130034,
            delta=130034 * 0.01,
        )

    def test_herdr_ceiling_clears_the_value_it_crashed_at(self) -> None:
        self.assertGreater(
            int(self.variables["cmd_center_herdr_tasks_max"]),
            CRASH_TASKS_MAX,
        )


class TemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.variables = _resolved_defaults()

    def _render_template(self, name: str) -> str:
        return _render((TEMPLATES / name).read_text(), self.variables)

    def test_herdr_unit_states_both_ceilings_in_the_service_section(self) -> None:
        rendered = self._render_template("herdr.service.j2")
        service = rendered.split("[Service]", 1)[1].split("[Install]", 1)[0]
        self.assertIn(
            f"TasksMax={self.variables['cmd_center_herdr_tasks_max']}", service
        )
        self.assertIn(
            f"LimitNPROC={self.variables['cmd_center_herdr_limit_nproc']}", service
        )

    def test_manager_drop_in_states_absolute_values(self) -> None:
        rendered = self._render_template("system-task-limits.conf.j2")
        self.assertIn("[Manager]", rendered)
        self.assertIn(
            f"DefaultTasksMax={self.variables['cmd_center_default_tasks_max']}", rendered
        )
        self.assertIn(
            f"DefaultLimitNPROC={self.variables['cmd_center_default_limit_nproc']}",
            rendered,
        )
        # A percentage here would resolve against the wrong threads-max at boot,
        # which is the whole reason this file exists.
        self.assertNotIn("%", rendered.split("[Manager]", 1)[1])

    def test_user_slice_drop_in_uses_the_slice_section(self) -> None:
        rendered = self._render_template("user-slice-tasks-max.conf.j2")
        self.assertIn("[Slice]", rendered)
        self.assertIn(
            f"TasksMax={self.variables['cmd_center_user_slice_tasks_max']}", rendered
        )


class TaskFileTest(unittest.TestCase):
    """Wiring. A drop-in in the wrong directory is silently inert."""

    def test_drop_in_destinations(self) -> None:
        wanted = {
            "system-task-limits.conf.j2": "/etc/systemd/system.conf.d/50-task-limits.conf",
            "user-slice-tasks-max.conf.j2": "/etc/systemd/system/user-.slice.d/50-tasks-max.conf",
        }
        found = {
            task["ansible.builtin.template"]["src"]: task["ansible.builtin.template"]["dest"]
            for task in yaml.safe_load(TASKS.read_text())
            if "ansible.builtin.template" in task
        }
        self.assertEqual(found, wanted)

    def test_drop_ins_notify_a_daemon_reload(self) -> None:
        for task in yaml.safe_load(TASKS.read_text()):
            if "ansible.builtin.template" in task:
                self.assertEqual(task.get("notify"), "Reload systemd", task["name"])

    def test_handler_exists(self) -> None:
        handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text())
        self.assertIn("Reload systemd", [h["name"] for h in handlers])

    def test_ceilings_are_raised_before_the_herdr_unit_is_deployed(self) -> None:
        imports = [
            task["ansible.builtin.import_tasks"]
            for task in yaml.safe_load(MAIN.read_text())
            if "ansible.builtin.import_tasks" in task
        ]
        self.assertIn("task_limits.yml", imports)
        self.assertLess(imports.index("task_limits.yml"), imports.index("herdr.yml"))


class LiveCeilingTest(unittest.TestCase):
    """The ceiling the assert trusts has to be the one the kernel is running.

    Codex flagged this on #206: cmd_center can run without vm_baseline (the
    documented DR path), and a computed ~130000 says nothing about a kernel
    still sitting at the 1 GiB value of 6847. Handing out DefaultTasksMax=8192
    there puts the clone() failures straight back.
    """

    def setUp(self) -> None:
        self.tasks = yaml.safe_load(TASKS.read_text())
        self.variables = _resolved_defaults()

    def _named(self, needle: str) -> list:
        return [t for t in self.tasks if needle in t.get("name", "")]

    def test_reads_both_the_enforced_and_the_persisted_value(self) -> None:
        sources = {
            t["ansible.builtin.slurp"]["src"]
            for t in self.tasks
            if "ansible.builtin.slurp" in t
        }
        # /proc is what the kernel is running; /etc/sysctl.conf is what the next
        # boot will use, and lowering that is the silent regression.
        self.assertIn("/proc/sys/kernel/threads-max", sources)
        self.assertIn("/etc/sysctl.conf", sources)

    def test_persists_the_ceiling_on_every_run(self) -> None:
        repair = [t for t in self.tasks if "ansible.posix.sysctl" in t]
        self.assertEqual(len(repair), 1, "expected exactly one sysctl task")
        args = repair[0]["ansible.posix.sysctl"]
        self.assertEqual(args["name"], "kernel.threads-max")
        self.assertTrue(args["sysctl_set"])
        self.assertEqual(args["state"], "present")
        # No `when`. A ceiling raised by hand with sysctl -w and never written to
        # a file passes a live check and regresses at the next boot, so the
        # idempotent write runs every time.
        self.assertNotIn("when", repair[0])

    def test_live_assert_rejects_a_kernel_ceiling_below_the_limits(self) -> None:
        conditions = _task(TASKS, LIVE_ASSERT_TASK)["ansible.builtin.assert"]["that"]
        broken = dict(self.variables, cmd_center_kernel_threads_max_live=6847)
        results = [_render("{{ " + c + " }}", broken) for c in conditions]
        self.assertIn(
            "False",
            results,
            "a kernel ceiling of 6847 under DefaultTasksMax=8192 must fail the assert",
        )

    def test_live_assert_is_skipped_under_check_mode(self) -> None:
        # The sysctl is not applied under --check, so asserting the live value
        # there would fail for the wrong reason.
        self.assertEqual(
            _task(TASKS, LIVE_ASSERT_TASK)["when"], "not ansible_check_mode"
        )

    def test_nothing_is_written_before_the_configuration_is_checked(self) -> None:
        """The review case: a bad target must be rejected before it is persisted.

        With an override of 8000 and a live ceiling of 10000, writing first put
        10000 into /etc/sysctl.conf, overwriting whatever higher value was there,
        and failed the play afterwards.
        """
        names = [t.get("name", "") for t in self.tasks]
        writes = [
            i
            for i, t in enumerate(self.tasks)
            if "ansible.posix.sysctl" in t or "ansible.builtin.template" in t
        ]
        config_assert = names.index(ASSERT_TASK)
        self.assertTrue(writes, "no write tasks found, test would be vacuous")
        self.assertLess(
            config_assert,
            min(writes),
            "the configuration assert must run before anything touches the host",
        )


class PersistedCeilingTest(unittest.TestCase):
    """The chosen ceiling, evaluated through Ansible's own templating.

    Three sources have to be compared, and the review of #210 found each one in
    turn: the value the RAM warrants, the value in force, and the value already
    written to /etc/sysctl.conf. Lowering any of them is a regression, the last
    one silently, at the next boot.

    Rendered with Templar rather than bare Jinja2 on purpose. Two bugs in this
    expression only showed up that way: an unbalanced parenthesis, and `\\s` in a
    folded YAML scalar reaching the regex as a literal backslash so the persisted
    value never matched. Both produced a plausible-looking number.
    """

    def setUp(self) -> None:
        try:
            from ansible.parsing.dataloader import DataLoader  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("ansible-core is not importable")
        self.expression = _task(TASKS, TARGET_TASK)["ansible.builtin.set_fact"][
            "cmd_center_threads_max_target"
        ]

    def _render(self, computed: int, live: int, sysctl_conf: str) -> int:
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar

        variables = {
            "cmd_center_kernel_threads_max": computed,
            "cmd_center_threads_max_before": {
                "content": base64.b64encode(f"{live}\n".encode()).decode()
            },
            "cmd_center_sysctl_conf": {
                "content": base64.b64encode(sysctl_conf.encode()).decode()
            },
        }
        return int(Templar(loader=DataLoader(), variables=variables).template(self.expression))

    def test_cases(self) -> None:
        none = "net.ipv4.ip_forward=1\n"
        low = "kernel.threads-max=6847\n"
        high = "net.core.somaxconn=1024\nkernel.threads-max = 200000\n"
        commented = "# kernel.threads-max=999999\nkernel.threads-max=130032\n"
        cases = [
            ("hotplug host, nothing persisted", 130032, 6847, none, 130032),
            ("steady state", 130032, 130032, low, 130032),
            # The review case: the file is ahead of the running kernel.
            ("persisted above computed and live", 130032, 130000, high, 200000),
            ("live raised by hand, file stale", 130032, 500000, low, 500000),
            # A low override must not win; the ordering assert rejects it instead.
            ("low override cannot lower", 8000, 10000, none, 10000),
            ("a commented line is not a value", 130032, 6847, commented, 130032),
            ("no sysctl.conf content", 130032, 6847, "", 130032),
        ]
        for name, computed, live, conf, expected in cases:
            with self.subTest(name):
                self.assertEqual(self._render(computed, live, conf), expected)


if __name__ == "__main__":
    unittest.main()
