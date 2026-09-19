"""pg_hba.conf must keep authorising the Kubernetes nodes.

`roles/postgresql/templates/pg_hba.conf.j2` renders the ENTIRE file from
`postgresql_hba_entries`. Anything missing from that list is not merely
absent, it is actively deleted from the host on the next run.

On 2026-05-03 Grafana went down because Postgres was rejecting connections
from the Kubernetes pods. The fix, three `host all all <node-ip>/32` lines,
was applied to the live host and never written back to the role. Verified on
2026-09-19: the live `/etc/postgresql/*/main/pg_hba.conf` carried those three
lines and the role defaults did not, so running `playbooks/postgresql.yml`
would have silently reverted the fix.

The 10.244.0.0/16 pod-CIDR rule does NOT cover this. A pod connecting to an
address outside the cluster is SNATed to its node's IP, so Postgres sees the
node address, never a pod address.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "roles" / "postgresql"
DEFAULTS = ROLE / "defaults" / "main.yml"
TEMPLATE = ROLE / "templates" / "pg_hba.conf.j2"

# The three Kubernetes nodes, from inventory: k8cluster2, k8cluster1, k8cluster3.
K8S_NODE_IPS = ("192.168.1.89", "192.168.1.90", "192.168.1.91")


def render_pg_hba() -> str:
    defaults = yaml.safe_load(DEFAULTS.read_text())
    entries = defaults["postgresql_hba_entries"]
    auth = defaults["postgresql_auth_method"]

    # trim_blocks matches ansible.builtin.template's own default, so what this
    # renders is what would land on the host rather than a lookalike.
    env = Environment(keep_trailing_newline=True, trim_blocks=True)
    # The entries themselves contain Jinja ({{ postgresql_auth_method }}), so
    # resolve them before rendering the file template.
    resolved = []
    for e in entries:
        resolved.append(
            {k: env.from_string(str(v)).render(postgresql_auth_method=auth) for k, v in e.items()}
        )
    return env.from_string(TEMPLATE.read_text()).render(postgresql_hba_entries=resolved)


class PgHbaKubernetesNodeTests(unittest.TestCase):
    def test_every_k8s_node_is_authorised(self):
        """The regression that took Grafana down on 2026-05-03."""
        rendered = render_pg_hba()
        for ip in K8S_NODE_IPS:
            self.assertIn(
                f"{ip}/32",
                rendered,
                f"{ip} missing: a run of playbooks/postgresql.yml would delete its "
                f"pg_hba line and reject that node's pods",
            )

    def test_k8s_node_rules_are_host_type_and_use_the_auth_method(self):
        """A rule that exists but is malformed is worse than a missing one."""
        defaults = yaml.safe_load(DEFAULTS.read_text())
        by_addr = {
            e.get("address", "").split("/")[0]: e
            for e in defaults["postgresql_hba_entries"]
            if e.get("type") == "host"
        }
        for ip in K8S_NODE_IPS:
            self.assertIn(ip, by_addr, f"no host rule for {ip}")
            entry = by_addr[ip]
            self.assertEqual(entry["method"], "{{ postgresql_auth_method }}")
            self.assertEqual(entry["address"], f"{ip}/32")

    def test_pod_cidr_rule_is_not_treated_as_covering_the_nodes(self):
        """Documents why the broad pod-CIDR rule does not make these redundant.

        If someone later deletes the node rules believing 10.244.0.0/16 covers
        them, this test and its message are the argument against it: pod to
        off-cluster traffic is SNATed to the node IP.
        """
        rendered = render_pg_hba()
        self.assertIn("10.244.0.0/16", rendered, "pod CIDR rule went missing")
        for ip in K8S_NODE_IPS:
            self.assertIn(f"{ip}/32", rendered)

    def test_template_renders_the_whole_file(self):
        """Why omissions are deletions rather than no-ops.

        If this ever stops being true (an append-only or blockinfile approach),
        the reasoning in this module needs revisiting.
        """
        body = TEMPLATE.read_text()
        self.assertIn("for entry in postgresql_hba_entries", body)
        self.assertNotIn("BEGIN ANSIBLE MANAGED BLOCK", body)


if __name__ == "__main__":
    unittest.main()
