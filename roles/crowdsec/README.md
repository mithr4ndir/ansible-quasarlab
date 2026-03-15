# CrowdSec Role

Deploys CrowdSec collaborative IPS on the NPM (Nginx Proxy Manager) host to
detect and automatically block malicious IPs at the firewall level.

## Architecture
CrowdSec runs as a Docker container on the NPM VM, sharing access to NPM's
proxy-host logs and the host's syslog/auth.log. A host-level firewall bouncer
(iptables) enforces ban decisions.

## What This Role Does
1. **CrowdSec container** — log analysis engine reading NPM + syslog
2. **Collections** — nginx-proxy-manager, http-cve, linux, whitelist-good-actors
3. **Whitelist** — internal CIDRs (192.168.1.0/24, K8s pod CIDR) are never banned
4. **Firewall bouncer** — iptables-based bouncer auto-blocks banned IPs on the host

## Collections Installed
- `crowdsecurity/nginx-proxy-manager` — NPM access log parsing + scenarios
- `crowdsecurity/http-cve` — common CVE exploit patterns (PHP probes, Log4Shell, etc.)
- `crowdsecurity/linux` — SSH brute force, syslog analysis
- `crowdsecurity/whitelist-good-actors` — excludes known good bots (Googlebot, etc.)

## Verify
```bash
ansible-playbook playbooks/crowdsec.yml

# Then on the NPM host:
docker exec crowdsec cscli metrics
systemctl status crowdsec-firewall-bouncer
iptables -L -n | grep crowdsec
```

## Safety
Internal CIDRs are whitelisted before the bouncer is enabled. Verify
with `docker exec crowdsec cscli parsers list | grep whitelist`.
