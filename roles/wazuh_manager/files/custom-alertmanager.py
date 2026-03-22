#!/usr/bin/env python3
"""
Wazuh custom integration: forward alerts to Prometheus Alertmanager.
Placed in /var/ossec/integrations/custom-alertmanager
Called by Wazuh integratord when rules match the configured level threshold.
"""

import json
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

ALERT_FILE = sys.argv[1]
HOOK_URL = sys.argv[3]

# Alertmanager API endpoint
ALERTMANAGER_URL = f"{HOOK_URL}/api/v2/alerts"

# Map Wazuh levels to severity labels
def wazuh_level_to_severity(level):
    if level >= 12:
        return "critical"
    elif level >= 10:
        return "warning"
    else:
        return "info"

def main():
    with open(ALERT_FILE) as f:
        alert = json.load(f)

    wazuh_alert = alert.get("parameters", {}).get("alert", alert)

    rule = wazuh_alert.get("rule", {})
    agent = wazuh_alert.get("agent", {})
    level = rule.get("level", 0)
    severity = wazuh_level_to_severity(level)

    rule_groups = rule.get("groups", [])
    group_str = ", ".join(rule_groups) if rule_groups else "general"

    alertmanager_alert = [{
        "labels": {
            "alertname": f"WazuhAlert_{rule.get('id', 'unknown')}",
            "severity": severity,
            "source": "wazuh",
            "instance": agent.get("name", "wazuh-manager"),
            "agent_ip": agent.get("ip", "unknown"),
            "rule_id": str(rule.get("id", "")),
            "rule_group": group_str,
        },
        "annotations": {
            "summary": rule.get("description", "Wazuh alert"),
            "description": (
                f"Rule: {rule.get('id', '')} (Level {level})\n"
                f"Agent: {agent.get('name', 'manager')} ({agent.get('ip', 'local')})\n"
                f"Groups: {group_str}\n"
                f"Full log: {wazuh_alert.get('full_log', 'N/A')[:500]}"
            ),
        },
        "startsAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }]

    data = json.dumps(alertmanager_alert).encode("utf-8")
    req = urllib.request.Request(
        ALERTMANAGER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except urllib.error.URLError as e:
        # Log failure but don't crash integratord
        sys.stderr.write(f"Failed to send to Alertmanager: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
