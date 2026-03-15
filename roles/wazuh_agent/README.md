# Wazuh Agent Role

Deploys the Wazuh agent to all Linux hosts for centralized SIEM monitoring,
file integrity checking, CIS benchmark compliance, and software inventory.

## What This Role Does
1. **Installs wazuh-agent** from the official Wazuh repository
2. **Configures OSSEC** — auto-enrolls with the manager using hostname and group tags
3. **Enables FIM** — monitors /etc, /usr/bin, /usr/sbin, /bin, /sbin, /boot
4. **Enables SCA** — CIS benchmark scanning every 12h
5. **Enables Syscollector** — hardware, OS, network, package, port, and process inventory every 1h

## Target Hosts
Deployed to all `linux` group hosts except `security` (the manager itself) and `nas`.
Agent group assignment is controlled by `wazuh_agent_groups` (default: `linux`,
overridden per group in group_vars, e.g. `linux,kubernetes` for K8s nodes).

## Deploy
```bash
ansible-playbook playbooks/wazuh.yml
```
