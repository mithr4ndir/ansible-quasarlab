# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
"""community.proxmox.proxmox, minus guest agent calls to VMs that cannot answer.

Upstream fetches agent/network-get-interfaces for every QEMU VM whose config has
`agent: 1`, whether or not the VM is running (it does gate the LXC equivalent on
status). A stopped VM has no agent, so Proxmox answers 500, upstream swallows
the HTTPError, and the host ends up with no interfaces either way. The only
effect of the call is a 500 in pveproxy's access.log on every inventory
resolution, which Wazuh raises as rules 31122/31162 (issue #226). Still true on
upstream main as of 2.1.0.

So this plugin skips the call unless the VM is running and, for QEMU, its QMP
status is also running. The second half covers vzdump: it holds a stopped VM
in QMP `prelaunch` to back it up (status `running`, measured on 101 and 106 on
2026-10-15), and a guest that is not executing cannot answer its agent. A
`paused` VM is the same case. The resulting inventory is
identical to upstream's; only the doomed request is gone.

Everything else, options included, is upstream's. Delete this file and point
inventory.proxmox.yml back at community.proxmox.proxmox once upstream gates the
agent call on status.
"""

from __future__ import annotations

from ansible.utils.display import Display
from ansible_collections.community.proxmox.plugins.inventory.proxmox import (
    DOCUMENTATION as _UPSTREAM_DOCUMENTATION,
)
from ansible_collections.community.proxmox.plugins.inventory.proxmox import (
    InventoryModule as _UpstreamInventoryModule,
)

display = Display()

_UPSTREAM_NAME = "community.proxmox.proxmox"
NAME = "quasarlab_proxmox"


def _rename(doc: str) -> str:
    # Options come from DOCUMENTATION at load time, and the `plugin` option's
    # choices are validated against the name in the inventory file, so both
    # spots must carry this plugin's name. Fail at import rather than load a
    # plugin that silently rejects its own inventory file.
    renamed = doc.replace("\n    name: proxmox\n", f"\n    name: {NAME}\n", 1)
    renamed = renamed.replace(f"choices: ['{_UPSTREAM_NAME}']", f"choices: ['{NAME}']", 1)
    if renamed.count(NAME) < 2:
        raise ImportError(f"{NAME}: upstream {_UPSTREAM_NAME} DOCUMENTATION changed shape, cannot rename")
    return renamed


DOCUMENTATION = _rename(_UPSTREAM_DOCUMENTATION)


class InventoryModule(_UpstreamInventoryModule):
    NAME = NAME

    def __init__(self) -> None:
        super().__init__()
        self._agent_can_answer = True

    def _get_vm_config(self, properties, node, vmid, vmtype, name):
        # Upstream only calls _get_agent_network_interfaces from here, after
        # _get_vm_status has filled in status and qmpstatus.
        status = properties.get(self._fact("status"))
        qmpstatus = properties.get(self._fact("qmpstatus"), status)
        self._agent_can_answer = status == "running" and (vmtype != "qemu" or qmpstatus == "running")
        try:
            return super()._get_vm_config(properties, node, vmid, vmtype, name)
        finally:
            self._agent_can_answer = True

    def _get_agent_network_interfaces(self, node, vmid, vmtype):
        if not self._agent_can_answer:
            display.vv(f"Skipping guest agent interfaces for {vmtype}/{vmid} on {node}: VM is not running")
            return []
        return super()._get_agent_network_interfaces(node, vmid, vmtype)
