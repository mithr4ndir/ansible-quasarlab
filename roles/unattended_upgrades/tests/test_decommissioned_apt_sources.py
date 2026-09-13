"""Regression tests for removing the orphaned Elastic 8.x apt repo fleet-wide.

Run from the repo root:
    uv run --with pytest pytest roles/unattended_upgrades/tests

The defect: the ELK decom left the Elastic 8.x repo (and its key in the legacy
/etc/apt/trusted.gpg) on every host the old filebeat role reached. The removal
was added only to wazuh_manager and pve/common, so command-center1 still had
both on 2026-09-13. That repo is what walked filebeat to 8.x on the Wazuh
host and blinded the SIEM for four months.

trusted-elastic-only.gpg is a byte copy of command-center1's /etc/apt/trusted.gpg
as apt_key left it (public key only).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = ROLE / "files" / "remove_apt_trusted_key.py"

ELASTIC_REPO = "/etc/apt/sources.list.d/artifacts_elastic_co_packages_8_x_apt.list"
ELASTIC_FPR = "46095ACC8548582C1A2699A9D27D666CD88E42B4"
UNRELATED_KEY = REPO / "roles" / "cmd_center" / "tests" / "fixtures" / "hashicorp-D55C0D1A-2026.asc"
UNRELATED_FPR = "D55C0D1AC78A8D8126CB631CFC9CA96ACA026560"

needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is required")


def load_yaml(path: Path):
    """PyYAML if importable, else the system python3's copy. Never skips."""
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


def run_script(keyring: Path, *fingerprints: str) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(SCRIPT), "--keyring", str(keyring)]
    for fpr in fingerprints:
        args += ["--fingerprint", fpr]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def primary_fprs(keyring: Path) -> list[str]:
    with tempfile.TemporaryDirectory() as homedir:
        out = subprocess.run(
            ["gpg", "--homedir", homedir, "--batch", "--with-colons", "--show-keys", str(keyring)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    fprs, want = [], False
    for line in out.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            want = True
        elif fields[0] == "fpr" and want:
            fprs.append(fields[9])
            want = False
    return fprs


def changed_when(stdout: str) -> bool:
    """Evaluate the task's changed_when the way Ansible's search test does."""
    tasks = load_yaml(ROLE / "tasks" / "decommissioned_apt_sources.yml")
    task = next(t for t in tasks if "ansible.builtin.script" in t)
    match = re.search(r"search\('(.+?)', multiline=True\)", task["changed_when"])
    assert match, task["changed_when"]
    return re.search(match.group(1), stdout, re.MULTILINE) is not None


# --- repo file removal: path, placement, reach ------------------------------


def test_defaults_target_the_exact_path_measured_on_command_center1() -> None:
    defaults = load_yaml(ROLE / "defaults" / "main.yml")
    assert defaults["unattended_upgrades_decommissioned_apt_sources"] == [ELASTIC_REPO]
    assert defaults["unattended_upgrades_decommissioned_trusted_keys"] == [ELASTIC_FPR]


def test_repo_removal_is_file_absent_over_the_defaults_list() -> None:
    tasks = load_yaml(ROLE / "tasks" / "decommissioned_apt_sources.yml")
    removal = next(t for t in tasks if "ansible.builtin.file" in t)
    assert removal["ansible.builtin.file"] == {"path": "{{ item }}", "state": "absent"}
    assert removal["loop"] == "{{ unattended_upgrades_decommissioned_apt_sources }}"


def test_repo_removal_replay_is_idempotent_and_only_touches_the_target(tmp_path: Path) -> None:
    """Replay file: state=absent against a copy of command-center1's sources.list.d."""
    root = tmp_path
    sources = root / "etc/apt/sources.list.d"
    sources.mkdir(parents=True)
    measured = {
        "artifacts_elastic_co_packages_8_x_apt.list": "deb https://artifacts.elastic.co/packages/8.x/apt stable main\n",
        "hashicorp.list": "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com noble main\n",
        "wazuh.list": "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main\n",
    }
    for name, body in measured.items():
        (sources / name).write_text(body)

    defaults = load_yaml(ROLE / "defaults" / "main.yml")
    results = []
    for _ in range(2):
        changed = False
        for item in defaults["unattended_upgrades_decommissioned_apt_sources"]:
            target = root / item.lstrip("/")
            if target.exists():
                target.unlink()
                changed = True
        results.append(changed)

    assert results == [True, False]
    assert sorted(p.name for p in sources.iterdir()) == ["hashicorp.list", "wazuh.list"]


def test_cleanup_runs_before_the_roles_first_apt_cache_update() -> None:
    tasks = load_yaml(ROLE / "tasks" / "main.yml")
    first_import = next(i for i, t in enumerate(tasks) if "ansible.builtin.import_tasks" in t)
    first_update = next(
        i for i, t in enumerate(tasks) if (t.get("ansible.builtin.apt") or {}).get("update_cache")
    )
    assert tasks[first_import]["ansible.builtin.import_tasks"] == "decommissioned_apt_sources.yml"
    assert first_import < first_update


def roles_applied(playbook: Path) -> list[tuple[str, str, object]]:
    applied = []
    for play in load_yaml(playbook):
        for role in play.get("roles") or []:
            name = role if isinstance(role, str) else role["role"]
            when = None if isinstance(role, str) else role.get("when")
            applied.append((play["hosts"], name, when))
    return applied


def test_role_reaches_every_apt_managed_host_including_proxmox() -> None:
    """The reason the cleanup lives here and not in pve/common or wazuh_manager."""
    monitoring = roles_applied(REPO / "playbooks" / "monitoring.yml")
    assert ("linux", "unattended_upgrades", "'nas' not in group_names") in monitoring
    vm_baseline = roles_applied(REPO / "playbooks" / "vm_baseline.yml")
    assert ("linux:!proxmox:!nas", "unattended_upgrades", None) in vm_baseline
    runner = (REPO / "scripts" / "run-proxmox.sh").read_text()
    assert re.search(r"for playbook in .*\bvm_baseline\.yml\b.*\bmonitoring\.yml\b", runner)


def test_no_other_role_carries_a_copy_of_the_removal() -> None:
    carriers = sorted(
        str(p.relative_to(REPO))
        for p in REPO.glob("roles/**/*.yml")
        if "artifacts_elastic_co_packages_8_x_apt" in p.read_text()
    )
    assert carriers == ["roles/unattended_upgrades/defaults/main.yml"]


# --- legacy trusted.gpg key removal, real gpg -------------------------------


@pytest.fixture
def elastic_only(tmp_path: Path) -> Path:
    keyring = tmp_path / "trusted.gpg"
    shutil.copyfile(FIXTURES / "trusted-elastic-only.gpg", keyring)
    keyring.chmod(0o644)
    return keyring


@needs_gpg
def test_fixture_matches_the_measured_host_state(elastic_only: Path) -> None:
    assert primary_fprs(elastic_only) == [ELASTIC_FPR]


@needs_gpg
def test_elastic_only_keyring_is_removed_then_idempotent(elastic_only: Path) -> None:
    first = run_script(elastic_only, ELASTIC_FPR)
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("removed:")
    assert changed_when(first.stdout)
    assert not elastic_only.exists()

    second = run_script(elastic_only, ELASTIC_FPR)
    assert second.returncode == 0, second.stderr
    assert second.stdout.startswith("unchanged:")
    assert not changed_when(second.stdout)


@needs_gpg
def test_unrelated_keys_in_trusted_gpg_are_kept(tmp_path: Path, elastic_only: Path) -> None:
    unrelated = tmp_path / "unrelated.gpg"
    with open(UNRELATED_KEY, "rb") as fin, open(unrelated, "wb") as fout:
        subprocess.run(["gpg", "--dearmor"], stdin=fin, stdout=fout, check=True)
    elastic_only.write_bytes(elastic_only.read_bytes() + unrelated.read_bytes())
    assert sorted(primary_fprs(elastic_only)) == sorted([ELASTIC_FPR, UNRELATED_FPR])

    first = run_script(elastic_only, ELASTIC_FPR)
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("changed:")
    assert changed_when(first.stdout)
    assert primary_fprs(elastic_only) == [UNRELATED_FPR]
    assert elastic_only.stat().st_mode & 0o777 == 0o644

    second = run_script(elastic_only, ELASTIC_FPR)
    assert second.stdout.startswith("unchanged:")
    assert not changed_when(second.stdout)


@needs_gpg
def test_missing_trusted_gpg_is_a_no_op(tmp_path: Path) -> None:
    result = run_script(tmp_path / "trusted.gpg", ELASTIC_FPR)
    assert result.returncode == 0
    assert result.stdout.startswith("unchanged:")
    assert not changed_when(result.stdout)


@needs_gpg
def test_unparseable_keyring_is_never_rewritten(tmp_path: Path) -> None:
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"\x00not a keyring\x00" * 8)
    before = keyring.read_bytes()
    result = run_script(keyring, ELASTIC_FPR)
    assert result.returncode == 2
    assert keyring.read_bytes() == before
