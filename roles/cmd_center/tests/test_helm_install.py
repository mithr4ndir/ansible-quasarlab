"""Regression tests for the pinned helm install in tasks/cli_tools.yml.

Run from the repo root:
    uv run --with pytest pytest roles/cmd_center/tests

The defect (#156): the helm archive was extracted to a fixed /tmp path behind
`creates: /usr/local/bin/helm-v<version>`, and the copy out of /tmp had no
matching guard. After a reboot cleared /tmp, a host that already had the
versioned binary skipped the extract and then failed the copy with `Source
/tmp/linux-amd64/helm not found`. Under --check the copy failed on every host.
The extract also ran as root with tar's default --same-owner, so the staging
tree took the uid recorded in the tarball (1001 on command-center1).

No playbook is run. A small driver walks the helm tasks from the YAML in order
and hands each one to the real Ansible module (stat, tempfile, unarchive,
copy, file) the way the task executor would: `when` and `changed_when` go
through Ansible's Conditional, arguments through its Templar, block/always
and register are honoured, and check mode is passed to each module as
_ansible_check_mode, so a module without check mode support skips itself.
Paths under /usr/local/bin and /tmp are rewritten into a temporary root, and
the helm download URL is rewritten to a local tarball. A URL with no tarball
maps to a missing file, so any unexpected download fails the run.

Owner and group root cannot be applied without root, so they are dropped for
the driver and pinned by the static test instead. Tar's ownership handling is
checked for real under fakeroot.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1]
CLI_TOOLS = ROLE / "tasks" / "cli_tools.yml"
# The interpreter running these tests. A hard-coded /usr/bin/python3 bypassed
# the environment `uv run --with ansible-core` provides, so on a clean host the
# helpers could not import ansible and every case was silently skipped.
SYSTEM_PYTHON = sys.executable
ARCH = "amd64"
PINNED = "3.19.0"
TARBALL_UID = 1001

DRIVER = r"""
import json, os, subprocess, sys
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.conditional import Conditional
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

init_plugin_loader()
cfg = json.load(sys.stdin)
root = cfg["root"]
loader = DataLoader()
variables = dict(cfg["vars"], ansible_check_mode=cfg["check_mode"])
MODULES = {
    "ansible.builtin.stat": "stat",
    "ansible.builtin.tempfile": "tempfile",
    "ansible.builtin.unarchive": "unarchive",
    "ansible.builtin.copy": "copy",
    "ansible.builtin.file": "file",
}
log = []


def cond(expressions, extra=None):
    all_vars = dict(variables, **(extra or {}))
    c = Conditional(loader=loader)
    c.when = expressions
    return c.evaluate_conditional(Templar(loader=loader, variables=all_vars), all_vars)


def rewrite(value):
    if isinstance(value, list):
        return [rewrite(v) for v in value]
    if isinstance(value, dict):
        return {k: rewrite(v) for k, v in value.items()}
    if not isinstance(value, str) or value.startswith(root):
        return value
    if "://" in value:
        return cfg["urls"].get(value, root + "/no-such-download.tar.gz")
    for prefix in ("/usr/local/bin", "/tmp"):
        if value == prefix or value.startswith(prefix + "/"):
            return root + value
    return value


def module_env():
    env = {"PATH": "/usr/bin:/bin", "HOME": root, "TMPDIR": root + "/tmp",
           "LANG": "C.UTF-8", "ANSIBLE_CONFIG": root + "/ansible.cfg"}
    # Keep fakeroot's preload and session key when the driver runs under it.
    env.update({k: v for k, v in os.environ.items() if k.startswith(("FAKEROOT", "LD_"))})
    return env


def execute(task):
    key = next(k for k in task if k in MODULES or k == "ansible.builtin.debug")
    args = Templar(loader=loader, variables=variables).template(task[key])
    check = variables["ansible_check_mode"] if "check_mode" not in task else bool(task["check_mode"])
    if key == "ansible.builtin.debug":
        return {"changed": False, "msg": args.get("msg")}
    module = MODULES[key]
    args = rewrite(args)
    if not cfg["as_root"]:
        for field in ("owner", "group"):
            if args.get(field) == "root":
                del args[field]
    if module == "unarchive" and args.get("creates"):
        # The unarchive action plugin, not the module, honours creates.
        if os.path.exists(args["creates"]):
            return {"changed": False, "skipped": True, "msg": "creates exists"}
    args["_ansible_check_mode"] = check
    args["_ansible_remote_tmp"] = root + "/ansible-remote-tmp"
    argfile = os.path.join(root, "module-args.json")
    with open(argfile, "w") as fh:
        json.dump({"ANSIBLE_MODULE_ARGS": args}, fh)
    proc = subprocess.run([sys.executable, "-m", "ansible.modules." + module, argfile],
                          env=module_env(), capture_output=True, text=True, cwd=root)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        return {"failed": True, "msg": proc.stdout + proc.stderr}
    if proc.returncode != 0:
        result["failed"] = True
    return result


def run(tasks, inherited):
    for task in tasks:
        when = task.get("when", [])
        when = inherited + (when if isinstance(when, list) else [when])
        if "block" in task:
            failed = run(task["block"], when)
            if run(task.get("always", []), when) or failed:
                return True
            continue
        if not cond(when):
            result, status = {"changed": False, "skipped": True}, "skipped"
        else:
            result = execute(task)
            register = {task["register"]: result} if task.get("register") else {}
            if "changed_when" in task and not result.get("skipped"):
                cw = task["changed_when"]
                result["changed"] = cond(cw if isinstance(cw, list) else [cw], register)
            if result.get("failed"):
                status = "failed"
            elif result.get("skipped"):
                status = "skipped"
            else:
                status = "changed" if result.get("changed") else "ok"
        if task.get("register"):
            variables[task["register"]] = result
        log.append({"name": task["name"], "status": status, "result": result})
        if status == "failed":
            return True
    return False


tasks = yaml.safe_load(open(cfg["tasks_file"]))
selected = [t for t in tasks if "helm" in t["name"].lower() and not t["name"].startswith("Verify")]
failed = run(selected, [])
report = {}
for path in cfg.get("report", []):
    try:
        st = os.lstat(path)
        report[path] = {"uid": st.st_uid}
    except FileNotFoundError:
        report[path] = None
print(json.dumps({"failed": failed, "log": log, "report": report}))
"""


def _ansible_available() -> bool:
    return Path(SYSTEM_PYTHON).exists() and subprocess.run(
        [SYSTEM_PYTHON, "-c", "import ansible.modules.unarchive, yaml"],
        capture_output=True,
        check=False,
    ).returncode == 0


if not _ansible_available():
    raise RuntimeError(
        f"{SYSTEM_PYTHON} cannot import ansible-core and pyyaml; run the documented "
        "command: uv run --with pytest --with pyyaml --with \"ansible-core==2.16.3\" pytest roles/cmd_center/tests"
    )


def load_tasks() -> list[dict]:
    proc = subprocess.run(
        [SYSTEM_PYTHON, "-c",
         "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
         str(CLI_TOOLS)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def flatten(tasks: list[dict]) -> list[dict]:
    out = []
    for task in tasks:
        if "block" in task:
            out.append(task)
            for key in ("block", "rescue", "always"):
                out.extend(flatten(task.get(key, [])))
        else:
            out.append(task)
    return out


def helm_tasks() -> list[dict]:
    top = [t for t in load_tasks()
           if "helm" in t["name"].lower() and not t["name"].startswith("Verify")]
    return flatten(top)


def url(version: str) -> str:
    return f"https://get.helm.sh/helm-v{version}-linux-{ARCH}.tar.gz"


def make_tarball(path: Path, version: str) -> bytes:
    """A helm release shaped archive, owned by a non-root uid like the real one."""
    binary = f"#!/bin/sh\necho v{version}+gfake\n".encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in (
            (f"linux-{ARCH}/helm", binary, 0o755),
            (f"linux-{ARCH}/LICENSE", b"license\n", 0o644),
            (f"linux-{ARCH}/README.md", b"readme\n", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            info.uid = info.gid = TARBALL_UID
            tar.addfile(info, io.BytesIO(data))
    return binary


class Host:
    """A fake filesystem root for one scenario."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "host"
        self.bin = self.root / "usr" / "local" / "bin"
        self.tmp = self.root / "tmp"
        self.bin.mkdir(parents=True)
        self.tmp.mkdir()
        (self.root / "ansible.cfg").write_text("[defaults]\n")
        self.urls: dict[str, str] = {}
        self.binaries: dict[str, bytes] = {}

    def versioned(self, version: str = PINNED) -> Path:
        return self.bin / f"helm-v{version}"

    @property
    def link(self) -> Path:
        return self.bin / "helm"

    def publish(self, version: str = PINNED) -> None:
        tarball = self.root.parent / f"helm-v{version}-linux-{ARCH}.tar.gz"
        self.binaries[version] = make_tarball(tarball, version)
        self.urls[url(version)] = str(tarball)

    def installed(self, version: str = PINNED) -> bytes:
        """State after an earlier successful run: binary and link, /tmp untouched."""
        data = f"installed helm {version}\n".encode()
        self.versioned(version).write_bytes(data)
        self.versioned(version).chmod(0o755)
        if self.link.is_symlink() or self.link.exists():
            self.link.unlink()
        self.link.symlink_to(self.versioned(version))
        return data

    def run(self, check_mode: bool = False, version: str = PINNED, as_root: bool = False,
            report: list[str] | None = None, wrapper: list[str] | None = None) -> dict:
        cfg = {
            "root": str(self.root),
            "tasks_file": str(CLI_TOOLS),
            "check_mode": check_mode,
            "as_root": as_root,
            "urls": self.urls,
            "vars": {"helm_version": version, "helm_arch": ARCH},
            "report": report or [],
        }
        proc = subprocess.run(
            [*(wrapper or []), SYSTEM_PYTHON, "-c", DRIVER],
            input=json.dumps(cfg), capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                 "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
                 "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-local-tmp")},
            cwd=self.root,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def tmp_entries(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir())


def statuses(outcome: dict) -> dict[str, str]:
    return {entry["name"]: entry["status"] for entry in outcome["log"]}


def failures(outcome: dict) -> list:
    return [(e["name"], e["result"].get("msg")) for e in outcome["log"] if e["status"] == "failed"]


def changed(outcome: dict) -> list[str]:
    return [e["name"] for e in outcome["log"] if e["status"] == "changed"]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


# --- apply -------------------------------------------------------------------


def test_fresh_host_installs_pinned_binary_and_link(host: Host) -> None:
    host.publish()
    outcome = host.run()
    assert not outcome["failed"], failures(outcome)
    assert host.versioned().read_bytes() == host.binaries[PINNED]
    assert stat.S_IMODE(host.versioned().stat().st_mode) == 0o755
    assert host.link.is_symlink()
    assert os.readlink(host.link) == str(host.versioned())
    # The staging directory is gone and nothing else was left in /tmp.
    assert host.tmp_entries() == []


def test_post_reboot_with_binary_present_and_staging_absent(host: Host) -> None:
    # Nothing is published, so any download or extract attempt fails the run.
    before = host.installed()
    assert host.tmp_entries() == []
    outcome = host.run()
    assert not outcome["failed"], failures(outcome)
    assert changed(outcome) == []
    assert host.versioned().read_bytes() == before
    assert os.readlink(host.link) == str(host.versioned())


def test_rerun_after_install_changes_nothing(host: Host) -> None:
    host.publish()
    assert not host.run()["failed"]
    installed = host.versioned().read_bytes()
    outcome = host.run()
    assert not outcome["failed"], failures(outcome)
    assert changed(outcome) == []
    assert host.versioned().read_bytes() == installed
    assert host.tmp_entries() == []


def test_regular_file_at_helm_path_is_replaced_by_the_link(host: Host) -> None:
    # command-center1 had a pre-role regular file at /usr/local/bin/helm.
    host.publish()
    host.link.write_bytes(b"old helm from 2025\n")
    outcome = host.run()
    assert not outcome["failed"], failures(outcome)
    assert host.link.is_symlink()
    assert os.readlink(host.link) == str(host.versioned())


def test_version_bump_installs_new_binary_and_moves_link(host: Host) -> None:
    old = host.installed(PINNED)
    host.publish("3.20.0")
    outcome = host.run(version="3.20.0")
    assert not outcome["failed"], failures(outcome)
    assert host.versioned("3.20.0").read_bytes() == host.binaries["3.20.0"]
    assert os.readlink(host.link) == str(host.versioned("3.20.0"))
    assert host.versioned(PINNED).read_bytes() == old
    assert host.tmp_entries() == []


def test_failed_download_cleans_up_and_installs_nothing(host: Host) -> None:
    corrupt = host.root.parent / "corrupt.tar.gz"
    corrupt.write_bytes(b"not a tarball")
    host.urls[url(PINNED)] = str(corrupt)
    outcome = host.run()
    assert outcome["failed"]
    assert not host.versioned().exists()
    assert host.tmp_entries() == []


# --- check mode --------------------------------------------------------------


def test_check_mode_on_fresh_host_reports_install_and_writes_nothing(host: Host) -> None:
    host.publish()
    outcome = host.run(check_mode=True)
    assert not outcome["failed"], failures(outcome)
    assert changed(outcome), "check mode must report the pending install"
    assert sorted(p.name for p in host.bin.iterdir()) == []
    assert host.tmp_entries() == []


def test_check_mode_post_reboot_is_clean(host: Host) -> None:
    host.installed()
    outcome = host.run(check_mode=True)
    assert not outcome["failed"], failures(outcome)
    assert changed(outcome) == []


def test_check_mode_downloads_nothing(host: Host) -> None:
    # No tarball is published: a check run that tried to fetch would fail.
    outcome = host.run(check_mode=True)
    assert not outcome["failed"], failures(outcome)
    for entry in outcome["log"]:
        if entry["name"].lower().startswith(("extract", "install helm binary")):
            assert entry["status"] == "skipped", entry


# --- ownership and layout ----------------------------------------------------


@pytest.mark.skipif(shutil.which("fakeroot") is None, reason="fakeroot is required")
def test_extract_as_root_does_not_keep_tarball_uid(host: Host, tmp_path: Path) -> None:
    """Run the extract task's own arguments through unarchive as (fake) root."""
    host.publish()
    extract = [t for t in helm_tasks() if "ansible.builtin.unarchive" in t]
    assert len(extract) == 1
    args = dict(extract[0]["ansible.builtin.unarchive"])
    args.pop("creates", None)
    dest = tmp_path / "staging"
    dest.mkdir()
    args.update(src=host.urls[url(PINNED)], dest=str(dest), remote_src=True)
    rendered = json.loads(json.dumps(args).replace("{{ helm_arch }}", ARCH))
    argfile = tmp_path / "unarchive-args.json"
    argfile.write_text(json.dumps({"ANSIBLE_MODULE_ARGS": rendered}))
    helm = dest / f"linux-{ARCH}" / "helm"
    script = (
        f'{SYSTEM_PYTHON} -m ansible.modules.unarchive "$1" >/dev/null || exit 9\n'
        f'stat -c %u "$2"\n'
    )
    proc = subprocess.run(
        ["fakeroot", "sh", "-c", script, "sh", str(argfile), str(helm)],
        capture_output=True, text=True, check=False, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "0", "extracted files kept the tarball uid"


def test_task_layout_is_pinned() -> None:
    tasks = helm_tasks()
    text = json.dumps(tasks)
    # No fixed staging path in a world-writable directory.
    assert '"/tmp' not in text
    extract = next(t for t in tasks if "ansible.builtin.unarchive" in t)["ansible.builtin.unarchive"]
    assert "--no-same-owner" in extract.get("extra_opts", [])
    assert extract["src"] == "https://get.helm.sh/helm-v{{ helm_version }}-linux-{{ helm_arch }}.tar.gz"
    copy = next(t for t in tasks if "ansible.builtin.copy" in t)["ansible.builtin.copy"]
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0755")
    assert copy["dest"] == "/usr/local/bin/helm-v{{ helm_version }}"
    link = next(t for t in tasks if t.get("ansible.builtin.file", {}).get("state") == "link")
    assert link["ansible.builtin.file"]["src"] == "/usr/local/bin/helm-v{{ helm_version }}"
    assert link["ansible.builtin.file"]["dest"] == "/usr/local/bin/helm"
    assert link["ansible.builtin.file"]["force"] is True
    tempdirs = [t for t in tasks if "ansible.builtin.tempfile" in t]
    assert tempdirs and tempdirs[0]["ansible.builtin.tempfile"]["state"] == "directory"
