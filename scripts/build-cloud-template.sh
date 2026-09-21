#!/usr/bin/env bash
#
# Build a Proxmox cloud-init template from an official cloud image.
#
# Runs ON a Proxmox node. Idempotent: if the target VMID already exists the
# script refuses to touch it, so re-running is safe and never silently
# clobbers a template other VMs were cloned from.
#
# The image is checksum-verified against the distro's own signed sums file
# before it is ever imported. An unverified cloud image becomes every VM in
# the lab, so this is not optional.
#
# Deliberately does NOT set ipconfig0. Addresses are per-VM and come from
# Terraform, so a template that carried ip=dhcp would quietly hand DHCP to
# anything cloned from it. The 2026-09-20 outage started with DHCP drift, so
# the template stays address-less and the caller must be explicit.
#
# Usage:
#   build-cloud-template.sh --distro debian13  --vmid 9001 --sshkeys /path/keys.pub
#   build-cloud-template.sh --distro ubuntu24  --vmid 9002 --sshkeys /path/keys.pub
#
set -euo pipefail

LOG_PREFIX="[build-cloud-template]"
STORAGE="${STORAGE:-truenas-iscsi}"
CIUSER="${CIUSER:-ladino}"
DISK_SIZE="${DISK_SIZE:-32G}"
MEMORY="${MEMORY:-2048}"
CORES="${CORES:-2}"
BRIDGE="${BRIDGE:-vmbr0}"
WORKDIR="${WORKDIR:-/var/lib/vz/template/cache}"

die() { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }
log() { echo "${LOG_PREFIX} $*"; }

DISTRO="" VMID="" SSHKEYS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --distro)  DISTRO="${2:-}"; shift 2 ;;
        --vmid)    VMID="${2:-}"; shift 2 ;;
        --sshkeys) SSHKEYS="${2:-}"; shift 2 ;;
        --storage) STORAGE="${2:-}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$DISTRO" ]]  || die "--distro is required (debian13|ubuntu24)"
[[ -n "$VMID" ]]    || die "--vmid is required"
[[ -n "$SSHKEYS" ]] || die "--sshkeys is required"
[[ -r "$SSHKEYS" ]] || die "ssh keys file not readable: $SSHKEYS"
[[ "$VMID" =~ ^[0-9]+$ ]] || die "--vmid must be numeric, got: $VMID"
grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-)' "$SSHKEYS" || die "no public keys found in $SSHKEYS"

case "$DISTRO" in
    debian13)
        NAME="debian-13-cloud-init-template"
        BASE="https://cloud.debian.org/images/cloud/trixie/latest"
        IMAGE="debian-13-genericcloud-amd64.qcow2"
        SUMS="SHA512SUMS"; SUMTOOL="sha512sum"
        ;;
    ubuntu24)
        NAME="ubuntu-24-04-cloud-init-template"
        BASE="https://cloud-images.ubuntu.com/noble/current"
        IMAGE="noble-server-cloudimg-amd64.img"
        SUMS="SHA256SUMS"; SUMTOOL="sha256sum"
        ;;
    *) die "unsupported --distro: $DISTRO (want debian13 or ubuntu24)" ;;
esac

command -v qm >/dev/null             || die "qm not found, run this on a Proxmox node"
command -v virt-customize >/dev/null || die "virt-customize not found, install libguestfs-tools"
pvesm status --storage "$STORAGE" >/dev/null 2>&1 || die "storage not available: $STORAGE"

if qm status "$VMID" >/dev/null 2>&1; then
    die "VMID $VMID already exists. Refusing to overwrite. Destroy it first if that is really what you want."
fi

mkdir -p "$WORKDIR"
cd "$WORKDIR"

# Always fetch the sums file, it is small and it is the authority.
curl -fsSL -o "$SUMS.tmp" "$BASE/$SUMS" || die "checksum file download failed"
expected="$(awk -v f="$IMAGE" '$2 == f || $2 == "*"f {print $1; exit}' "$SUMS.tmp")"
rm -f "$SUMS.tmp"
[[ -n "$expected" ]] || die "no checksum entry for $IMAGE in $SUMS"

# Reuse a cached image only when it still matches the published checksum.
if [[ -f "$IMAGE" ]] && [[ "$($SUMTOOL "$IMAGE" | awk '{print $1}')" == "$expected" ]]; then
    log "reusing verified cached $IMAGE"
else
    log "downloading $IMAGE"
    curl -fsSL -o "$IMAGE.tmp" "$BASE/$IMAGE" || die "image download failed"
    actual="$($SUMTOOL "$IMAGE.tmp" | awk '{print $1}')"
    if [[ "$expected" != "$actual" ]]; then
        rm -f "$IMAGE.tmp"
        die "CHECKSUM MISMATCH for $IMAGE. expected=$expected actual=$actual"
    fi
    log "checksum verified ($SUMTOOL)"
    mv "$IMAGE.tmp" "$IMAGE"
fi

# Bake qemu-guest-agent into a COPY, leaving the verified download pristine
# so the cache stays checksum-comparable on the next run.
#
# This is not cosmetic. The Proxmox dynamic inventory derives ansible_host
# from proxmox_agent_interfaces, so a VM whose guest agent is missing has no
# ansible_host, silently drops out of the `linux` group, and disappears from
# both Ansible and the generated Prometheus targets. Neither Debian's
# genericcloud image nor Ubuntu's cloud image ships the agent, and a smoke
# test of the first build of this template reproduced exactly that: cloud-init
# reported done, `qm agent <id> ping` got nothing.
CUSTOM="${IMAGE}.custom"
rm -f "$CUSTOM"
cp "$IMAGE" "$CUSTOM"
log "installing qemu-guest-agent into the image"
virt-customize -a "$CUSTOM" \
    --install qemu-guest-agent \
    --run-command 'systemctl enable qemu-guest-agent' \
    >/dev/null 2>&1 || { rm -f "$CUSTOM"; die "virt-customize failed to install qemu-guest-agent"; }

log "creating VM $VMID ($NAME) on $STORAGE"
qm create "$VMID" \
    --name "$NAME" \
    --memory "$MEMORY" \
    --cores "$CORES" \
    --cpu host \
    --numa 1 \
    --net0 "virtio,bridge=${BRIDGE}" \
    --ostype l26 \
    --scsihw virtio-scsi-single \
    --agent enabled=1 \
    --serial0 socket \
    --vga serial0

# Roll back a half-built VM rather than leaving a broken shell behind.
cleanup_partial() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "${LOG_PREFIX} build failed (rc=$rc), destroying partial VM $VMID" >&2
        qm destroy "$VMID" --purge >/dev/null 2>&1 || true
        rm -f "${CUSTOM:-}" 2>/dev/null || true
    fi
    return $rc
}
trap cleanup_partial EXIT

# Parse the volume id out of importdisk rather than assuming the naming
# scheme. It differs between storage backends (lvmthin, zfs, dir), and
# guessing it wrong silently attaches nothing.
import_out="$(qm importdisk "$VMID" "$CUSTOM" "$STORAGE" 2>&1)" || {
    echo "$import_out" >&2; die "importdisk failed"
}
volid="$(printf '%s' "$import_out" | sed -n "s/.*[Ii]mported disk \\(as \\)\\{0,1\\}'\\([^']*\\)'.*/\\2/p" | tail -1)"
volid="${volid#unused0:}"
[[ -n "$volid" ]] || { echo "$import_out" >&2; die "could not determine imported volume id"; }
log "imported disk as $volid"
qm set "$VMID" --scsi0 "${volid},discard=on,iothread=1,ssd=1,backup=1" >/dev/null
qm disk resize "$VMID" scsi0 "$DISK_SIZE" >/dev/null
qm set "$VMID" --ide2 "${STORAGE}:cloudinit" >/dev/null
qm set "$VMID" --boot "order=scsi0" >/dev/null
qm set "$VMID" --ciuser "$CIUSER" >/dev/null
qm set "$VMID" --sshkeys "$SSHKEYS" >/dev/null
qm set "$VMID" --ciupgrade 1 >/dev/null
qm set "$VMID" --nameserver "192.168.1.1" >/dev/null
# No ipconfig0 on purpose. See the header.

qm template "$VMID" >/dev/null
rm -f "$CUSTOM"
trap - EXIT

log "template $VMID ready: $NAME"
qm config "$VMID" | grep -E '^(name|template|scsi0|ide2|ciuser|agent|ipconfig0|boot)' || true
