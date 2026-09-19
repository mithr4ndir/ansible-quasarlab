# uptime_kuma role

Uptime Kuma on vm117 (`uptime-kuma`, 192.168.1.129), the lab's monitor from
**outside** the Kubernetes cluster. Applied by `playbooks/uptime-kuma.yml`
through `scripts/run-uptime-kuma.sh`. Issue #178.

The in-cluster alert pipeline cannot report its own death: on 2026-09-19
Prometheus and Loki were down for about 11 hours and nothing alerted
(k8s-argocd#213). This host watches the cluster and the NAS from a VM whose
disk is not on NFS, and alerts straight to Discord.

## What it runs

| Piece | Details |
|---|---|
| Docker CE + compose plugin | From Docker's apt repo, signing key pinned by fingerprint. Replaces Debian's `docker.io`, which has no `docker compose` and is why the original labctl deploy never started Kuma. |
| `uptime-kuma` container | `louislam/uptime-kuma:2.5.5-slim-rootless`, digest-pinned, uid 1000, all capabilities dropped, SQLite in `/opt/uptime-kuma/data`. |
| `autokuma` container | `ghcr.io/bigboot/autokuma:2.0.0` (latest stable), digest-pinned, uid 65532. Reconciles the files in `/opt/uptime-kuma/monitors` into Kuma every 60s. No Docker socket. |
| `kuma-nfs-probe.timer` | Every 60s, a real NFS read of a sentinel file on `192.168.1.15:/mnt/tank/k8s`, pushed to a Kuma push monitor. |

## Monitors (defined in `defaults/main.yml`)

| Monitor | Check |
|---|---|
| Prometheus | `http://192.168.1.230:9090/-/ready` |
| Alertmanager | `http://192.168.1.233:9093/-/ready` |
| Grafana | `http://192.168.1.229/api/health` |
| Loki | `http://192.168.1.231:3100/ready` |
| Kubernetes API k8cluster1/2/3 | `https://<node>:6443/readyz` (includes etcd; anonymous by kubeadm default) |
| NFS read 192.168.1.15:/mnt/tank/k8s | push monitor fed by `kuma-nfs-probe` |

Every monitor notifies **Discord #alerts directly**, not through
`discord-alert-proxy`, which runs in the cluster being watched. HTTP monitors
alert after three failed checks (about 3 minutes) and repeat hourly while down.

## Monitors as code

Kuma keeps monitors only in its SQLite database. AutoKuma turns them into
files: `tasks/monitors.yml` renders one JSON file per entity from
`templates/entities.json.j2`, and AutoKuma creates, updates and deletes Kuma
entities to match. Losing Kuma's database loses history, not configuration.
Editing a monitor in the UI is reverted on the next sync; add or change
monitors in `uptime_kuma_http_monitors` instead.

Why AutoKuma and not `lucasheld.uptime_kuma`: the collection's last release
was 2023-08 and its client library's last release 2023-09, with an open
"This project seems to be abandoned" issue; it supports only Kuma 1.x, whose
last release (1.23.17) still carries GHSA-v832-4r73-wx5j, fixed only in 2.2.1.
AutoKuma is maintained and targets Kuma 2. The role pins its latest stable
release, 2.0.0; 2.1.x is still a release candidate. AutoKuma speaks Kuma's
unofficial socket.io API, so a Kuma upgrade can break it. That only stops
drift correction, never monitoring, and the deploy's verify step fails
loudly if it happens.

Known 2.0.0 limits, seen in the end-to-end test: a newly created monitor can
send its first DOWN notification twice (AutoKuma#166), the push monitor's
resend interval is not applied (#152, so the NFS monitor alerts once per
outage rather than hourly), and 2.0.0 cannot read a password from a file
reference in the environment, so it gets a mounted config file.

### Tested end to end (2026-09-19)

On cmd-center1, in throwaway containers from the pinned images, bound to
127.0.0.1, with Discord and the monitored endpoints replaced by a local
capture listener. The compose file, ownership and modes came from this
role's templates and tasks. Everything was removed afterwards.

- Kuma started on SQLite with no setup page; `kuma-admin.js` created the
  admin, a rerun changed nothing, and a wrong password was refused.
- AutoKuma 2.0.0 logged in from the config file (no password in
  `docker inspect`), created the notification and all monitors once each,
  every one wired to the notification.
- An HTTP monitor on an endpoint answering 500 with an apiserver-style
  `[-]etcd failed` body went DOWN and produced a Discord embed with
  "Request failed with status code 500".
- The real probe and real `nfs-cat` against a stalled fake NFS server pushed
  DOWN after its deadline, and the Discord embed carried the probe's reason.
  With pushes stopped, the push monitor went DOWN with "No heartbeat in the
  time window".
- After Kuma's data directory was wiped, bootstrap recreated the admin on
  the first connection and AutoKuma recreated every monitor and the
  notification; the verifier passed.

## The NFS probe

A TCP check on 2049 or an RPC ping would have stayed green on 2026-09-19:
the port answered, `rpcinfo` said "ready and waiting", `showmount -e`
listed every export, and every file operation hung. So the probe does a real
MOUNT, LOOKUP, GETATTR and READ of `.uptime-kuma-nfs-probe` at the export
root and compares the bytes.

It must also never hang vm117 the way the hung mounts wedged the k8s nodes.
So there is **no kernel mount**. `nfs-cat` (libnfs-utils) is a userspace NFS
client: when the server stalls it sits in an interruptible socket wait, and
the probe kills its whole process group at a 20 second deadline. systemd's
`TimeoutStartSec` is a second, outer bound. Tests drive the real `nfs-cat`
against a fake NFS server that answers MOUNT and then stalls (the 2026-09-19
shape) and show it is killed on time and nothing is left running.

Kuma learns the result two ways: the probe pushes UP or DOWN with the reason
on every run, so a failing read alerts after two consecutive DOWN pushes
(about two minutes). And the push monitor is a dead man's switch: if the
timer, the script or the VM stops, pushes stop and Kuma alerts after two
missed 150 second windows (about five minutes).

The probe runs under `DynamicUser` with only `CAP_NET_BIND_SERVICE`, because
the NAS's exports require a reserved source port. It presents AUTH_SYS uid 0
by default, as the k8s nodes do.

**Limits, stated plainly.** It reads with NFSv3 from a fourth client. The
2026-09-19 deadlock was NFSv4.1 session state on the server
(`nfsd4_destroy_session`), and k8cluster1 kept working throughout, so a fresh
client doing v3 reads may well have stayed green that day. What catches that
incident from here is the Prometheus and Loki readiness monitors. The NFS
probe catches the NAS or its NFS service failing for everyone. Catching one
client's stuck mount needs a probe on that client; see the follow-ups in the
PR for #178.

The only write this role makes to the NAS is at deploy time: if the sentinel
does not exist, it is created once with `nfs-cp`, which refuses to
overwrite. The timer only reads.

## Secrets

| Secret | Where it comes from | Where it lives |
|---|---|---|
| Discord #alerts webhook | 1Password item `vausmfy2q2m57r6scvziyrc7lq`, field `credential`, via the wrapper's `cached_op_read discord_alerts_webhook_url` | `/opt/uptime-kuma/monitors/discord-alerts.json` (0400, AutoKuma uid) and Kuma's database |
| Kuma admin password | Generated on the host at first deploy | `/opt/uptime-kuma/secrets/admin_password` (root:3001 0440, file secret in the Kuma container) and `autokuma.toml` (root:3001 0440, mounted into AutoKuma) |
| NFS push token | Generated on the host at first deploy | `/opt/uptime-kuma/secrets/nfs_push_token` (root 0400), given to the probe with `LoadCredential` |

The admin password never passes through Ansible: `kuma-admin.js` reads the
file secret inside the Kuma container, and AutoKuma's config file is built
from it on the host by a shell task. Log in as `admin` with
`sudo cat /opt/uptime-kuma/secrets/admin_password` on vm117.

A fresh Kuma makes the first visitor to `/setup` its admin. The role keeps
the port on 127.0.0.1 until the admin account exists and its login is
verified, then republishes it on the LAN.

## Running

```bash
scripts/run-uptime-kuma.sh --check
scripts/run-uptime-kuma.sh
```

The wrapper runs merged code only and uses `inventory.static.ini` alone, so it
works with the Proxmox API down. The play fails unless Kuma answers from the
host and from the controller, every monitor exists once and is wired to
Discord, and one NFS probe run reports UP.

## Verifying an alert end to end

In the Kuma UI, Settings, Notifications, **Test** on the Discord notification
sends one message. For a real alert, `sudo systemctl stop kuma-nfs-probe.timer`
on vm117: about five minutes later the NFS monitor alerts through the dead
man's switch. Start the timer again to clear it.
