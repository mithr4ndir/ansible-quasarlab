"""The Proxmox inventory must not ask a VM's guest agent for interfaces unless it can answer.

Run from the repo root with the pinned runner (the plugin needs requests):
    uv run --with pytest --with pyyaml --with jinja2 --with requests \
        --with "ansible-core==2.16.3" pytest -rs tests/test_proxmox_inventory_agent.py

Why this exists (issue #226). community.proxmox.proxmox calls
agent/network-get-interfaces for every QEMU VM with `agent: 1`, running or not.
For a stopped VM Proxmox answers 500, so every inventory resolution left 500s in
pveproxy's access.log for 101 and 106, and Wazuh raised them as rules
31122/31162. inventory_plugins/quasarlab_proxmox.py skips the call for a VM that
is not running, or is running but not executing (QMP prelaunch or paused, which
is how vzdump holds a stopped VM while backing it up).

SAFETY: this never resolves the real inventory and never reaches 1Password or a
real Proxmox host. It loads the committed inventory.proxmox.yml, rewrites only
the URL and the token secret, and points it at a stub API on 127.0.0.1 that
records every request. The token secret is a fixed placeholder, so neither
PROXMOX_TOKEN_SECRET nor the vault password is ever read.
"""

from __future__ import annotations

import configparser
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytest.importorskip("requests", reason="community.proxmox's inventory plugin needs requests; add --with requests")
pytest.importorskip("ansible")

from ansible import constants as C  # noqa: E402
from ansible.inventory.manager import InventoryManager  # noqa: E402
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.plugins.loader import inventory_loader  # noqa: E402
from ansible.utils.collection_loader._collection_finder import _AnsibleCollectionFinder  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LOCAL_PLUGIN = "quasarlab_proxmox"
UPSTREAM_PLUGIN = "community.proxmox.proxmox"

NODE = "pve2"
# vmid: (name, cluster-list status, status/current status, qmpstatus, tags)
VMS = {
    100: ("k8cluster1", "running", "running", "running", "linux;k8s"),
    106: ("palworld", "stopped", "stopped", "stopped", "linux"),
    # vzdump holding a stopped VM for its backup: running to Proxmox, prelaunch
    # to QMP (measured on 101 and 106 on 2026-10-15). Paused is handled the same.
    101: ("windows-2022", "running", "running", "prelaunch", "windows"),
    114: ("autopilot-win11", "running", "running", "paused", "windows"),
}
RUNNING_IP = "192.168.1.90"


def _agent_path(vmid: int) -> str:
    return f"/api2/json/nodes/{NODE}/qemu/{vmid}/agent/network-get-interfaces"


class _StubProxmox(BaseHTTPRequestHandler):
    """Answers the endpoints the plugin reads, the way Proxmox VE does."""

    requests: list[str] = []

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    def _send(self, code: int, data) -> None:
        body = json.dumps({"data": data}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        type(self).requests.append(path)
        base = f"/api2/json/nodes/{NODE}"
        if path == "/api2/json/nodes":
            return self._send(200, [{"node": NODE, "type": "node", "status": "online"}])
        if path == f"{base}/lxc" or path == "/api2/json/pools":
            return self._send(200, [])
        if path == f"{base}/qemu":
            return self._send(200, [{"vmid": v, "name": n, "status": s} for v, (n, s, *_) in VMS.items()])
        for vmid, (name, _, status, qmp, tags) in VMS.items():
            vm = f"{base}/qemu/{vmid}"
            if path == f"{vm}/status/current":
                return self._send(200, {"status": status, "qmpstatus": qmp})
            if path == f"{vm}/config":
                return self._send(200, {"name": name, "agent": "1", "tags": tags, "cores": 2})
            if path == f"{vm}/snapshot":
                return self._send(200, [{"name": "current"}])
            if path == _agent_path(vmid):
                if qmp != "running":
                    # What pveproxy logs as the 500 Wazuh alerts on.
                    return self._send(500, None)
                return self._send(200, {"result": [
                    {"name": "lo", "hardware-address": "00:00:00:00:00:00",
                     "ip-addresses": [{"ip-address": "127.0.0.1", "prefix": 8}]},
                    {"name": "eth0", "hardware-address": "bc:24:11:00:00:01",
                     "ip-addresses": [{"ip-address": RUNNING_IP, "prefix": 24}]},
                ]})
        return self._send(404, None)


@pytest.fixture(scope="module", autouse=True)
def _ansible_plugins():
    _AnsibleCollectionFinder(paths=C.COLLECTIONS_PATHS)._install()
    try:
        import ansible_collections.community.proxmox.plugins.inventory.proxmox  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"community.proxmox is not installed: {exc}")
    inventory_loader.add_directory(str(REPO / "inventory_plugins"))


@pytest.fixture()
def stub_url():
    _StubProxmox.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubProxmox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _resolve(tmp_path: Path, url: str, plugin: str) -> InventoryManager:
    config = yaml.safe_load((REPO / "inventory.proxmox.yml").read_text())
    assert config["plugin"] == LOCAL_PLUGIN, "inventory.proxmox.yml must use the local plugin"
    config.update(plugin=plugin, url=url, token_secret="not-a-real-secret")
    source = tmp_path / f"{plugin.replace('.', '_')}.proxmox.yml"
    source.write_text(yaml.safe_dump(config))
    inventory = InventoryManager(loader=DataLoader(), sources=[str(source)])
    # InventoryManager turns a plugin failure into a warning and an empty
    # inventory, so prove the source really parsed before asserting anything.
    assert {"k8cluster1", "palworld", "windows-2022", "autopilot-win11"} <= set(inventory.hosts), inventory.hosts
    return inventory


def test_upstream_plugin_calls_the_agent_of_stopped_vms(tmp_path, stub_url):
    # The defect, reproduced. If this starts failing, upstream has fixed it and
    # inventory_plugins/quasarlab_proxmox.py can be deleted.
    _resolve(tmp_path, stub_url, UPSTREAM_PLUGIN)
    assert _agent_path(106) in _StubProxmox.requests
    assert _agent_path(101) in _StubProxmox.requests
    assert _agent_path(114) in _StubProxmox.requests


def test_local_plugin_skips_the_agent_of_stopped_and_paused_vms(tmp_path, stub_url):
    _resolve(tmp_path, stub_url, LOCAL_PLUGIN)
    assert _agent_path(106) not in _StubProxmox.requests  # stopped
    assert _agent_path(101) not in _StubProxmox.requests  # prelaunch, held by vzdump
    assert _agent_path(114) not in _StubProxmox.requests  # paused
    # Facts are still fetched for them; only the agent call is gone.
    assert f"/api2/json/nodes/{NODE}/qemu/106/config" in _StubProxmox.requests


def test_local_plugin_still_reads_the_agent_of_running_vms(tmp_path, stub_url):
    inventory = _resolve(tmp_path, stub_url, LOCAL_PLUGIN)
    assert _agent_path(100) in _StubProxmox.requests
    k8 = inventory.get_host("k8cluster1").get_vars()
    assert k8["ansible_host"] == RUNNING_IP
    assert [g.name for g in inventory.get_host("k8cluster1").get_groups() if g.name == "linux"] == ["linux"]


def test_local_plugin_yields_the_same_inventory_as_upstream(tmp_path, stub_url):
    # Upstream swallows the 500, so skipping the call must change nothing else.
    upstream = _resolve(tmp_path, stub_url, UPSTREAM_PLUGIN)
    local = _resolve(tmp_path, stub_url, LOCAL_PLUGIN)

    def snapshot(inv: InventoryManager) -> dict:
        return {
            name: (
                {k: v for k, v in host.get_vars().items() if k not in ("inventory_file", "inventory_dir")},
                sorted(g.name for g in host.get_groups()),
            )
            for name, host in inv.hosts.items()
        }

    assert snapshot(local) == snapshot(upstream)


def test_ansible_cfg_puts_the_local_plugin_on_the_search_path():
    # The tests above add the directory themselves; production relies on this.
    cfg = configparser.ConfigParser()
    cfg.read(REPO / "ansible.cfg")
    paths = [p.strip() for p in cfg["defaults"]["inventory_plugins"].split(":")]
    assert "./inventory_plugins" in paths
    assert (REPO / "inventory_plugins" / f"{LOCAL_PLUGIN}.py").is_file()
