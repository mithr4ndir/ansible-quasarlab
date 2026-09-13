"""Regression tests for the HashiCorp apt key and source definition.

Run from the repo root:
    uv run --with pytest pytest roles/cmd_center/tests

The defect: HashiCorp rotated its Linux package signing key on 2026-09-10.
command-center1 kept a keyring holding only the 2023 key (798A...E701), so
every apt-get update failed with NO_PUBKEY FC9CA96ACA026560. The role's key
task was a one-shot get_url that never re-fetched.

The script tests use real gpg/gpgv against real fixtures: both HashiCorp keys
and the dists/noble/InRelease HashiCorp published on 2026-09-11. The YAML
tests pin the task wiring that the script cannot see.
"""

from __future__ import annotations

import fnmatch
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
SCRIPT = ROLE / "files" / "apt_pinned_key.py"

NEW_FPR = "D55C0D1AC78A8D8126CB631CFC9CA96ACA026560"
OLD_FPR = "798AEC654E5C15428C8E42EEAA16FCBCA621E701"
NEW_KEY = FIXTURES / "hashicorp-D55C0D1A-2026.asc"
OLD_KEY = FIXTURES / "hashicorp-798AEC65-2023.asc"
IN_RELEASE = FIXTURES / "noble-InRelease-2026-09-11"

needs_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgv") is None,
    reason="gpg and gpgv are required",
)


def load_yaml(path: Path):
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


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def dearmor(src: Path, dest: Path) -> Path:
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        subprocess.run(["gpg", "--dearmor"], stdin=fin, stdout=fout, check=True)
    return dest


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


def gpgv_verifies(keyring: Path) -> bool:
    return (
        subprocess.run(
            ["gpgv", "--keyring", str(keyring), str(IN_RELEASE)],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.fixture
def stale_keyring(tmp_path: Path) -> Path:
    """The exact state measured on command-center1: 2023 key only, binary."""
    return dearmor(OLD_KEY, tmp_path / "hashicorp-archive-keyring.gpg")


# --- script behaviour against real keys -----------------------------------


@needs_gpg
def test_stale_keyring_reproduces_the_no_pubkey_failure(stale_keyring: Path) -> None:
    assert primary_fprs(stale_keyring) == [OLD_FPR]
    assert not gpgv_verifies(stale_keyring)


@needs_gpg
def test_check_flags_stale_keyring_for_replacement(stale_keyring: Path) -> None:
    result = run_script("check", "--keyring", str(stale_keyring), "--fingerprint", NEW_FPR)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("stale:")


@needs_gpg
def test_check_flags_missing_keyring(tmp_path: Path) -> None:
    result = run_script("check", "--keyring", str(tmp_path / "nope.gpg"), "--fingerprint", NEW_FPR)
    assert result.returncode == 1
    assert result.stdout.startswith("missing:")


@needs_gpg
def test_check_ignores_subkeys_when_matching_primary(stale_keyring: Path) -> None:
    # The 2023 key carries a signing subkey; only the primary is compared, so
    # a correctly pinned keyring with subkeys must not re-install every run.
    result = run_script("check", "--keyring", str(stale_keyring), "--fingerprint", OLD_FPR)
    assert result.returncode == 0, result.stdout


@needs_gpg
def test_install_converges_stale_keyring_and_is_then_idempotent(stale_keyring: Path) -> None:
    result = run_script(
        "install", "--source", str(NEW_KEY), "--keyring", str(stale_keyring), "--fingerprint", NEW_FPR
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(f"changed: {stale_keyring} now holds {NEW_FPR}")
    assert primary_fprs(stale_keyring) == [NEW_FPR]
    assert stale_keyring.stat().st_mode & 0o777 == 0o644
    # The real upstream InRelease now verifies, which is what apt needs.
    assert gpgv_verifies(stale_keyring)

    # The role only enters the replace block on rc 1. A second check is rc 0,
    # so a second run does not download or write anything.
    again = run_script("check", "--keyring", str(stale_keyring), "--fingerprint", NEW_FPR)
    assert again.returncode == 0, again.stdout


@needs_gpg
def test_install_refuses_download_without_pinned_key(stale_keyring: Path) -> None:
    before = stale_keyring.read_bytes()
    result = run_script(
        "install", "--source", str(OLD_KEY), "--keyring", str(stale_keyring), "--fingerprint", NEW_FPR
    )
    assert result.returncode == 3
    assert "refusing to install" in result.stderr
    assert stale_keyring.read_bytes() == before


@needs_gpg
def test_install_exports_only_the_pinned_key_when_extras_are_served(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.asc"
    bundle.write_text(NEW_KEY.read_text() + OLD_KEY.read_text())
    keyring = tmp_path / "keyrings" / "hashicorp-archive-keyring.gpg"
    result = run_script("install", "--source", str(bundle), "--keyring", str(keyring), "--fingerprint", NEW_FPR)
    assert result.returncode == 0, result.stderr
    assert primary_fprs(keyring) == [NEW_FPR]


@needs_gpg
def test_check_flags_keyring_with_extra_keys(tmp_path: Path) -> None:
    keyring = tmp_path / "k.gpg"
    keyring.write_bytes(dearmor(NEW_KEY, tmp_path / "a").read_bytes() + dearmor(OLD_KEY, tmp_path / "b").read_bytes())
    result = run_script("check", "--keyring", str(keyring), "--fingerprint", NEW_FPR)
    assert result.returncode == 1


@needs_gpg
def test_rejects_short_key_id_as_fingerprint(stale_keyring: Path) -> None:
    result = run_script("check", "--keyring", str(stale_keyring), "--fingerprint", "FC9CA96ACA026560")
    assert result.returncode == 2


# --- role wiring ------------------------------------------------------------


def flatten(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from flatten(task.get(key))


def module_of(task: dict) -> str:
    for key in task:
        if key.startswith("ansible.builtin.") or key in {"copy", "apt_repository", "lineinfile", "template"}:
            return key.removeprefix("ansible.builtin.")
    return ""


def test_defaults_pin_the_verified_fingerprint() -> None:
    defaults = load_yaml(ROLE / "defaults" / "main.yml")
    assert defaults["hashicorp_apt_key_fingerprint"] == NEW_FPR
    assert defaults["hashicorp_apt_keyring"] == "/etc/apt/keyrings/hashicorp-archive-keyring.gpg"


def test_key_and_source_tasks_run_before_any_apt_cache_update() -> None:
    main = load_yaml(ROLE / "tasks" / "main.yml")
    imports = [t["ansible.builtin.import_tasks"] for t in main]
    assert imports[0] == "hashicorp_apt.yml"
    # packages.yml holds the update_cache that failed on command-center1.
    packages = load_yaml(ROLE / "tasks" / "packages.yml")
    assert any(
        (t.get("ansible.builtin.apt") or {}).get("update_cache") for t in packages
    ), "fixture assumption broken: packages.yml no longer updates the cache"
    assert imports.index("packages.yml") > 0
    for task in flatten(load_yaml(ROLE / "tasks" / "hashicorp_apt.yml")):
        args = task.get(f"ansible.builtin.{module_of(task)}")
        assert not (isinstance(args, dict) and "update_cache" in args), task["name"]


def test_replace_block_is_gated_on_check_result_and_uses_the_script() -> None:
    tasks = load_yaml(ROLE / "tasks" / "hashicorp_apt.yml")
    check = next(t for t in tasks if t.get("register") == "hashicorp_apt_key_check")
    assert check["ansible.builtin.script"]["cmd"].startswith("apt_pinned_key.py check")
    assert check["check_mode"] is False
    block = next(t for t in tasks if "block" in t)
    assert block["when"] == "hashicorp_apt_key_check.rc == 1"
    install = [t for t in block["block"] if "ansible.builtin.script" in t]
    assert len(install) == 1
    assert install[0]["ansible.builtin.script"]["cmd"].startswith("apt_pinned_key.py install")


def all_task_files() -> list[Path]:
    return sorted(REPO.glob("roles/**/tasks/*.yml")) + sorted(REPO.glob("playbooks/*.yml"))


def test_exactly_one_task_defines_a_hashicorp_apt_source() -> None:
    definers = []
    for path in all_task_files():
        doc = load_yaml(path) or []
        tasks = []
        for item in doc:
            if isinstance(item, dict) and "hosts" in item:
                for section in ("pre_tasks", "tasks", "post_tasks"):
                    tasks.extend(item.get(section) or [])
            else:
                tasks.append(item)
        for task in flatten(tasks):
            if not isinstance(task, dict):
                continue
            module = module_of(task)
            args = task.get(f"ansible.builtin.{module}", task.get(module))
            blob = str(args)
            names_repo = "apt.releases.hashicorp.com" in blob or "hashicorp_apt_repo_url" in blob
            writes = module in {"copy", "template", "apt_repository", "deb822_repository", "blockinfile"} or (
                module == "lineinfile" and isinstance(args, dict) and args.get("state", "present") == "present"
            )
            if names_repo and writes:
                definers.append((path.relative_to(REPO), task["name"], module, args))
    assert len(definers) == 1, definers
    path, _, module, args = definers[0]
    assert str(path) == "roles/cmd_center/tasks/hashicorp_apt.yml"
    assert module == "copy"
    assert args["dest"] == "/etc/apt/sources.list.d/hashicorp.list"


def render(template: str, variables: dict) -> str:
    def sub(match: re.Match) -> str:
        return str(variables[match.group(1)])

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template)


def active_hashicorp_entries(sources_dir: Path, sources_list: Path) -> list[str]:
    entries = []
    for path in sorted(sources_dir.iterdir()) + [sources_list]:
        for line in path.read_text().splitlines():
            if re.match(r"^\s*(deb(-src)?\s|URIs:).*apt\.releases\.hashicorp\.com", line):
                entries.append(f"{path.name}: {line.strip()}")
    return entries


def test_role_leaves_a_single_hashicorp_source_on_a_messy_host(tmp_path: Path) -> None:
    """Replay the role's source tasks against a host with every known variant.

    Uses the task's own content, find regex, patterns and excludes, applied
    with the same semantics as the modules (copy writes the whole file, find
    uses re.match per line, lineinfile uses re.search per line).
    """
    tasks = load_yaml(ROLE / "tasks" / "hashicorp_apt.yml")
    defaults = load_yaml(ROLE / "defaults" / "main.yml")
    variables = {**defaults, "ansible_distribution_release": "noble"}

    sources_dir = tmp_path / "sources.list.d"
    sources_dir.mkdir()
    sources_list = tmp_path / "sources.list"
    # Measured on command-center1 on 2026-09-13.
    (sources_dir / "hashicorp.list").write_text(
        "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com noble main\n"
    )
    (sources_dir / "vector.list").write_text(
        "deb [signed-by=/usr/share/keyrings/datadog-archive-keyring.gpg] https://apt.vector.dev/ stable vector-0\n"
    )
    # Other shapes the vendor docs or apt_repository can leave behind.
    (sources_dir / "apt_releases_hashicorp_com.list").write_text(
        "deb https://apt.releases.hashicorp.com noble main\n"
    )
    (sources_dir / "hashicorp.sources").write_text(
        "Types: deb\nURIs: https://apt.releases.hashicorp.com\nSuites: noble\nComponents: main\n"
    )
    (sources_dir / "notes.list").write_text("# deb https://apt.releases.hashicorp.com noble main\n")
    sources_list.write_text(
        "# comment\ndeb https://apt.releases.hashicorp.com noble main\n"
    )
    assert len(active_hashicorp_entries(sources_dir, sources_list)) == 4

    for _ in range(2):  # second pass proves the replay is idempotent
        copy = next(t for t in tasks if "ansible.builtin.copy" in t)["ansible.builtin.copy"]
        (sources_dir / Path(copy["dest"]).name).write_text(render(copy["content"], variables))

        find = next(t for t in tasks if "ansible.builtin.find" in t)["ansible.builtin.find"]
        pattern = re.compile(find["contains"])
        for path in sorted(sources_dir.iterdir()):
            if not any(fnmatch.fnmatch(path.name, p) for p in find["patterns"]):
                continue
            if any(fnmatch.fnmatch(path.name, p) for p in find["excludes"]):
                continue
            if any(pattern.match(line) for line in path.read_text().splitlines(keepends=True)):
                path.unlink()

        lineinfile = next(t for t in tasks if "ansible.builtin.lineinfile" in t)["ansible.builtin.lineinfile"]
        regex = re.compile(lineinfile["regexp"])
        kept = [ln for ln in sources_list.read_text().splitlines(keepends=True) if not regex.search(ln)]
        sources_list.write_text("".join(kept))

        entries = active_hashicorp_entries(sources_dir, sources_list)
        assert entries == [
            "hashicorp.list: deb [arch=amd64 signed-by=/etc/apt/keyrings/hashicorp-archive-keyring.gpg] "
            "https://apt.releases.hashicorp.com noble main"
        ]
        assert (sources_dir / "vector.list").exists()
        assert (sources_dir / "notes.list").exists()
