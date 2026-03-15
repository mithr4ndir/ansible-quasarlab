#!/usr/bin/env bash
#
# Bootstrap Vector on external/new devices to ship logs to the Aggregator.
# Usage: curl -fsSL <url>/bootstrap-vector.sh | sudo bash
#
set -euo pipefail

AGGREGATOR_HOST="${VECTOR_AGGREGATOR:-192.168.1.232}"
AGGREGATOR_PORT="${VECTOR_AGGREGATOR_PORT:-6000}"
HOSTNAME="${VECTOR_HOSTNAME:-$(hostname -s)}"

echo "==> Installing Vector..."
curl -fsSL https://keys.datadoghq.com/DATADOG_APT_KEY_CURRENT.public | gpg --dearmor -o /usr/share/keyrings/datadog-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/datadog-archive-keyring.gpg] https://apt.vector.dev/ stable vector-0" > /etc/apt/sources.list.d/vector.list
apt-get update -qq
apt-get install -y -qq vector

echo "==> Configuring Vector to ship to aggregator at ${AGGREGATOR_HOST}:${AGGREGATOR_PORT}..."
cat > /etc/vector/vector.yaml <<VECTOREOF
data_dir: /var/lib/vector

sources:
  journald:
    type: journald

  var_log:
    type: file
    include:
      - /var/log/*.log
      - /var/log/syslog
      - /var/log/auth.log
    read_from: end

transforms:
  remap:
    type: remap
    inputs:
      - journald
      - var_log
    source: |
      .job = "varlogs"
      .host = "${HOSTNAME}"
      file_path = to_string(.file) ?? ""
      if contains(file_path, "auth") {
        .log_type = "auth"
      } else if contains(file_path, "syslog") || exists(._SYSTEMD_UNIT) {
        .log_type = "syslog"
      } else if contains(file_path, "kern") {
        .log_type = "kernel"
      } else {
        .log_type = "application"
      }
      del(.source_type)
      del(.file)

sinks:
  aggregator:
    type: vector
    inputs:
      - remap
    address: "${AGGREGATOR_HOST}:${AGGREGATOR_PORT}"
VECTOREOF

echo "==> Enabling and starting Vector..."
systemctl enable --now vector
systemctl restart vector

echo "==> Done. Vector is shipping logs to ${AGGREGATOR_HOST}:${AGGREGATOR_PORT}"
