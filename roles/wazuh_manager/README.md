# Wazuh Manager Role

Deploys Wazuh all-in-one (Manager + Indexer + Dashboard) as a single-node
SIEM with file integrity monitoring, vulnerability detection, CIS benchmarks,
and software inventory.

## What This Role Does
1. **Wazuh Indexer** — OpenSearch-based backend for alert storage and search
2. **Wazuh Manager** — OSSEC-based HIDS: log analysis, FIM, rootcheck, SCA, vulnerability detection
3. **Wazuh Dashboard** — OpenSearch Dashboards UI for alert visualization
4. **Filebeat** — ships manager alerts to the indexer
5. **Certificate generation** — self-signed TLS certs for all components
6. **Credential rotation** — admin/API passwords pulled from 1Password

## Enabled Modules
- **File Integrity Monitoring (FIM)** — monitors /etc, /usr/bin, /usr/sbin, /bin, /sbin, /boot
- **Rootcheck** — rootkit, trojan, and system anomaly detection
- **Vulnerability Detection** — CVE matching against Canonical, Debian, and NVD feeds
- **Security Configuration Assessment (SCA)** — CIS benchmark scanning every 12h
- **Syscollector** — hardware, OS, network, package, port, and process inventory every 1h

## Access
- **Dashboard**: https://192.168.1.171:5601
- **API**: https://192.168.1.171:55000
- **Agent registration**: 192.168.1.171:1515
- **Agent communication**: 192.168.1.171:1514

## Credentials
All credentials are stored in the `Wazuh SIEM` item in the 1Password `Infrastructure` vault.

## Deploy
```bash
ansible-playbook playbooks/wazuh.yml
```
