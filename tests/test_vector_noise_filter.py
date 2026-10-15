"""The host Vector noise filter drops known-benign lines and nothing near them.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with jinja2 --with "ansible-core==2.16.3" \
        pytest -rs tests/test_vector_noise_filter.py

Renders roles/monitoring/vector/templates/vector.yaml.j2 for a Proxmox host and
a k8s node and runs Vector's own unit tests (`vector test`) against the result,
so the VRL is compiled and executed by the same engine that ships the logs.
`vector validate` is not enough: it accepted a condition that did not compile.

The "drop" messages are real lines copied from the journals on 2026-10-15
(issues #223, #227, #228). The "keep" messages are near misses: real iSCSI and
containerd errors from the same programs, plus edits of the dropped lines, all
of which must still reach Loki.

Skipped when the vector binary is not on PATH. It is installed on
command-center1, pinned to the same version as the fleet.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / "roles" / "monitoring" / "vector"

DROP = {
    "iscsi_lba_status": "kvm: iSCSI GET_LBA_STATUS failed at lba 221184: SENSE KEY:ILLEGAL_REQUEST(5) ASCQ:INVALID_FIELD_IN_CDB(0x2400)",
    "syslogin_logout": "syslogin_perform_logout: logout() returned an error",
    "networkd_cali": "ERROR:Failed to get interface \"cali63d17246d0c\" status: Command '['/usr/bin/networkctl', 'status', '--no-pager', '--no-legend', '--lines=0', '--', 'cali63d17246d0c']' returned non-zero exit status 1.",
    "containerd_notfound": 'time="2026-10-14T01:59:46.978728149Z" level=error msg="ContainerStatus for \\"ec6ff5e70a6ca72e3c44d0c9bad732cc4c79e8b8e4c3bfce274d6218c4ef20a5\\" failed" error="rpc error: code = NotFound desc = an error occurred when try to find container \\"ec6ff5e70a6ca72e3c44d0c9bad732cc4c79e8b8e4c3bfce274d6218c4ef20a5\\": not found"',
    "nvrm": "NVRM: No NVIDIA GPU found.",
}

KEEP = {
    "iscsi_task_set_full": "kvm: iSCSI Busy/TaskSetFull/TimeOut (retry #1 in 0 ms): TASK_SET_FULL",
    "iscsi_lba_status_other_sense": "kvm: iSCSI GET_LBA_STATUS failed at lba 221184: SENSE KEY:MEDIUM_ERROR(3) ASCQ:UNRECOVERED_READ_ERROR(0x1100)",
    "containerd_ttrpc_closed": 'time="2026-10-12T04:01:47.663536660Z" level=error msg="collecting metrics for 684615b995a7c02e41034c89e4eb0e8b8be33d49fd912aa7048c71848e4ddd3f" error="ttrpc: closed"',
    "containerd_other_code": DROP["containerd_notfound"].replace("code = NotFound", "code = Unavailable"),
    "syslogin_longer": DROP["syslogin_logout"] + " for user root",
    "networkd_eth0": DROP["networkd_cali"].replace("cali63d17246d0c", "eth0"),
}

pytestmark = pytest.mark.skipif(
    shutil.which("vector") is None, reason="vector binary not on PATH"
)


def render(tmp_path: Path, host: str, groups: list[str]) -> Path:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    # trim_blocks matches the Ansible template module's default.
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    template = env.from_string((ROLE / "templates" / "vector.yaml.j2").read_text())
    out = tmp_path / f"{host}.yaml"
    out.write_text(template.render(inventory_hostname=host, group_names=groups, **defaults))
    return out


def log_input(component: str, fields: dict) -> list[dict]:
    return [{"insert_at": component, "type": "log", "log_fields": fields}]


def filter_tests() -> list[dict]:
    tests = [
        {
            "name": f"drops {name}",
            "inputs": log_input("noise_filter", {"message": msg}),
            "no_outputs_from": ["noise_filter"],
        }
        for name, msg in DROP.items()
    ]
    tests += [
        {
            "name": f"keeps {name}",
            "inputs": log_input("noise_filter", {"message": msg}),
            "outputs": [{
                "extract_from": "noise_filter",
                "conditions": [{"type": "vrl", "source": f".message == {json.dumps(msg)}"}],
            }],
        }
        for name, msg in KEEP.items()
    ]
    tests.append({
        "name": "keeps entry without message",
        "inputs": log_input("noise_filter", {"SYSLOG_IDENTIFIER": "x"}),
        "outputs": [{
            "extract_from": "noise_filter",
            "conditions": [{"type": "vrl", "source": '.SYSLOG_IDENTIFIER == "x"'}],
        }],
    })
    return tests


def metric_tests() -> list[dict]:
    return [
        {
            "name": "counts iscsi lba status by host",
            "inputs": log_input("iscsi_lba_status", {"message": DROP["iscsi_lba_status"]}),
            "outputs": [{
                "extract_from": "iscsi_lba_status_metric",
                "conditions": [{"type": "vrl", "source": (
                    '.name == "qemu_iscsi_get_lba_status_failed_total" && .tags.host == "pve"'
                    ' && .type == "counter" && .kind == "incremental"'
                )}],
            }],
        },
        {
            "name": "does not count other iscsi errors",
            "inputs": log_input("iscsi_lba_status", {"message": KEEP["iscsi_task_set_full"]}),
            "no_outputs_from": ["iscsi_lba_status"],
        },
    ]


def run_vector_test(config: Path, tests: list[dict], tmp_path: Path) -> None:
    test_file = tmp_path / f"{config.stem}-tests.yaml"
    test_file.write_text(yaml.safe_dump({"tests": tests}, width=10000))
    result = subprocess.run(
        ["vector", "test", str(config), str(test_file)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # Guard against a run that compiled nothing and reported success.
    for test in tests:
        assert f"test {test['name']} ... passed" in output, output


def test_proxmox_host_filters_and_counts(tmp_path: Path) -> None:
    config = render(tmp_path, "pve", ["proxmox", "linux"])
    run_vector_test(config, filter_tests() + metric_tests(), tmp_path)


def test_k8s_node_filters_without_metrics(tmp_path: Path) -> None:
    config = render(tmp_path, "k8cluster1", ["k8s", "linux"])
    rendered = yaml.safe_load(config.read_text())
    # The exporter opens a port, so it exists only where the line can occur.
    assert "metrics" not in rendered["sinks"]
    assert "iscsi_lba_status_metric" not in rendered["transforms"]
    run_vector_test(config, filter_tests(), tmp_path)


def test_security_host_renders_with_wazuh_pipeline(tmp_path: Path) -> None:
    config = render(tmp_path, "wazuh", ["security", "linux"])
    rendered = yaml.safe_load(config.read_text())
    assert "wazuh_remap" in rendered["transforms"]
    run_vector_test(config, filter_tests(), tmp_path)
