"""Keep requirements.yml and the collections the repo actually uses in step.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest tests/test_requirements.py

Why this exists. Three kinds of drift had all already happened here:

1. Used but not declared. kubernetes.core (16 tasks), community.general
   (vm_baseline's timezone, kube_pkg's dpkg_selections) and
   community.postgresql (roles/postgresql) were absent from requirements.yml.
   Runs worked only because the control node happened to have them from the
   `ansible` package, so a control node bootstrapped from that file alone would
   have failed on those roles.
2. Declared but unused. community.grafana outlived its last use, and its
   declared floor of >=2.1.0 was not even met on the control node (1.7.0), so
   nothing was proving the pin.
3. Hidden behind a core redirect. roles/k8s/common writes
   `ansible.builtin.sysctl`, which ansible-core redirects to
   ansible.posix.sysctl. It reads like a core module and is really a dependency
   on a collection, so the scan resolves redirects rather than trusting the
   namespace it is written with.
4. Written as a value rather than a key. `action: ns.coll.module` and
   `local_action:` are valid module invocations where the FQCN is the value, so a
   key-shaped scan sees only the word `action`. None exist here today, which is
   exactly when to close the hole.
5. Not a task at all. inventory.proxmox.yml names the
   community.proxmox.proxmox inventory plugin, and ansible.cfg lists that file
   first, so without the collection a control node has no dynamically
   discovered groups: no k8s, no cmd_center, no security. A scan that only
   reads tasks passes happily while the whole inventory is unloadable.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.yml"

# Module keys only, and only in files where a module key is what a mapping key
# means. defaults/ and vars/ are excluded on purpose: `net.ipv4.tcp_tw_reuse:`
# in a sysctl dict is not a module.
# Both extensions: Ansible accepts .yaml as readily as .yml, and a single
# tasks/main.yaml would otherwise be a hole straight through this scan.
SCANNED_GLOBS = tuple(
    pattern.format(ext=ext)
    for ext in ("yml", "yaml")
    for pattern in (
        "roles/**/tasks/**/*.{ext}",
        "roles/**/handlers/**/*.{ext}",
        "playbooks/*.{ext}",
        # site.yml is a playbook too, and a collection-backed task added there
        # would otherwise be invisible to this scan while the syntax check
        # passed, because the collection happens to be installed on whatever
        # machine ran it.
        "site.{ext}",
    )
)
# The colon does not have to end the line: `ansible.builtin.command: /bin/true`
# and `ansible.builtin.shell: |` are both common here, and a collection module
# written that way was invisible to an earlier version of this pattern.
MODULE_KEY = re.compile(r"^\s*(?:-\s+)?([a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+):(?:\s|$)")
# `action: community.general.timezone name=UTC` and `local_action:` are valid
# module invocations where the FQCN is a VALUE, not a key, so the pattern above
# sees only the word `action`. Also the `module:` sub-key form of the same thing.
ACTION_VALUE = re.compile(
    r"^\s*(?:-\s+)?(?:local_)?action:\s*['\"]?([a-z0-9_]+\.[a-z0-9_]+)\.[a-z0-9_]+"
)
ACTION_MODULE_KEY = re.compile(
    r"^\s*module:\s*['\"]?([a-z0-9_]+\.[a-z0-9_]+)\.[a-z0-9_]+"
)
CORE_NAMESPACES = {"ansible.builtin", "ansible.legacy"}

# Inventory sources name their plugin by FQCN, and ansible.cfg's `inventory`
# line decides which files are loaded.
INVENTORY_GLOBS = ("inventory*.yml", "inventory*.yaml")
PLUGIN_KEY = re.compile(r"^\s*plugin:\s*['\"]?([a-z0-9_]+\.[a-z0-9_]+)\.([a-z0-9_]+)['\"]?\s*$")


def _core_module_redirects() -> dict:
    """ansible.builtin.<module> -> owning collection, from ansible-core itself."""
    spec = importlib.util.find_spec("ansible")
    if spec is None or not spec.submodule_search_locations:
        raise unittest.SkipTest("ansible-core is not importable")
    routing = (
        Path(spec.submodule_search_locations[0])
        / "config"
        / "ansible_builtin_runtime.yml"
    )
    if not routing.is_file():
        raise unittest.SkipTest(f"{routing} is missing")
    modules = yaml.safe_load(routing.read_text())["plugin_routing"]["modules"]
    redirects = {}
    for name, entry in modules.items():
        target = (entry or {}).get("redirect")
        if target and target.count(".") == 2:
            redirects[name] = target.rsplit(".", 1)[0]
    return redirects


def _declared() -> dict:
    return {
        c["name"]: c.get("version")
        for c in yaml.safe_load(REQUIREMENTS.read_text())["collections"]
    }


def _used(redirects: dict) -> dict:
    """collection -> sorted sightings, resolving core redirects."""
    found: dict[str, set] = {}
    for glob in SCANNED_GLOBS:
        for path in sorted(REPO.glob(glob)):
            for line in path.read_text().splitlines():
                namespace = module = None
                key_match = MODULE_KEY.match(line)
                if key_match:
                    namespace, _, module = key_match.group(1).rpartition(".")
                    fqcn = key_match.group(1)
                    shape = ""
                else:
                    for pattern, label in (
                        (ACTION_VALUE, " (action form)"),
                        (ACTION_MODULE_KEY, " (action module key)"),
                    ):
                        action_match = pattern.match(line)
                        if action_match:
                            namespace = action_match.group(1)
                            module = line.split(namespace + ".", 1)[1].split()[0]
                            module = module.split(":")[0].strip("'\"")
                            fqcn = f"{namespace}.{module}"
                            shape = label
                            break
                if namespace is None:
                    continue
                where = f"{path.relative_to(REPO)}: {fqcn}{shape}"
                if namespace in CORE_NAMESPACES:
                    owner = redirects.get(module)
                    if owner and owner not in CORE_NAMESPACES:
                        found.setdefault(owner, set()).add(where + " (core redirect)")
                    continue
                found.setdefault(namespace, set()).add(where)
    inventories = sorted(
        path for glob in INVENTORY_GLOBS for path in REPO.glob(glob)
    )
    for path in inventories:
        for line in path.read_text().splitlines():
            match = PLUGIN_KEY.match(line)
            if not match:
                continue
            namespace, plugin = match.group(1), match.group(2)
            if namespace in CORE_NAMESPACES:
                continue
            found.setdefault(namespace, set()).add(
                f"{path.relative_to(REPO)}: {namespace}.{plugin} (inventory plugin)"
            )
    return {k: sorted(v) for k, v in found.items()}


class RequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.declared = _declared()
        self.used = _used(_core_module_redirects())

    def test_every_used_collection_is_declared(self) -> None:
        missing = {c: self.used[c][:3] for c in self.used if c not in self.declared}
        self.assertEqual(
            missing,
            {},
            "used in a task or handler but absent from requirements.yml, so a "
            "control node bootstrapped from that file alone would fail",
        )

    def test_every_declared_collection_is_used(self) -> None:
        unused = sorted(c for c in self.declared if c not in self.used)
        self.assertEqual(
            unused,
            [],
            "declared in requirements.yml but referenced by no task, so nothing "
            "proves the pin is right",
        )

    def test_every_declaration_pins_a_floor(self) -> None:
        for name, version in self.declared.items():
            with self.subTest(collection=name):
                self.assertIsNotNone(version, "no version constraint")
                self.assertRegex(str(version), r"^(>=|==|>|~>)\s*\d+\.\d+")

    def test_redirect_resolution_is_live(self) -> None:
        # The redirect map is the subtle half of this test, so assert it is
        # really being read rather than silently empty.
        redirects = _core_module_redirects()
        self.assertEqual(redirects.get("sysctl"), "ansible.posix")
        self.assertEqual(redirects.get("timezone"), "community.general")

    def test_the_scan_finds_the_collections_we_know_are_there(self) -> None:
        # Guards against the module-key regex matching nothing, which would make
        # the first two assertions vacuously pass.
        for expected in ("kubernetes.core", "community.general", "ansible.posix"):
            self.assertIn(expected, self.used)

    def test_the_scan_covers_inventory_plugins(self) -> None:
        # The dynamic inventory is not a task, and missing it is what let
        # community.proxmox stay undeclared while every play still ran here.
        self.assertIn("community.proxmox", self.used)
        self.assertTrue(
            any("inventory plugin" in s for s in self.used["community.proxmox"]),
            self.used["community.proxmox"],
        )


if __name__ == "__main__":
    unittest.main()
