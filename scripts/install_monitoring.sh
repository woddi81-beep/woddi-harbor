#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$ROOT/data/secrets"
RUNTIME_DIR="$ROOT/data/runtime/monitoring"
TOKEN_FILE="$SECRETS_DIR/metrics.token"
ENV_FILE="$SECRETS_DIR/metrics.env"
CONFIG_FILE="$RUNTIME_DIR/prometheus.yml"
DATA_DIR="$RUNTIME_DIR/data"
IMAGE="prom/prometheus:v3.12.0"

command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}

umask 077
mkdir -p "$SECRETS_DIR" "$RUNTIME_DIR" "$DATA_DIR"
if [[ ! -s "$TOKEN_FILE" ]]; then
  openssl rand -hex 32 >"$TOKEN_FILE"
fi
TOKEN="$(tr -d '\r\n' <"$TOKEN_FILE")"
printf 'HARBOR_METRICS_TOKEN=%s\n' "$TOKEN" >"$ENV_FILE"
sed "s|__HARBOR_METRICS_TOKEN__|$TOKEN|g" "$ROOT/deploy/prometheus.local.yml.tpl" >"$CONFIG_FILE"

"$ROOT/harbor.sh" service install harbor --mode user --enable
systemctl --user restart woddi-harbor.service

if docker container inspect woddi-harbor-prometheus >/dev/null 2>&1; then
  docker rm -f woddi-harbor-prometheus >/dev/null
fi

docker run -d \
  --name woddi-harbor-prometheus \
  --restart unless-stopped \
  --network host \
  --user "$(id -u):$(id -g)" \
  --volume "$CONFIG_FILE:/etc/prometheus/prometheus.yml:ro" \
  --volume "$DATA_DIR:/prometheus" \
  "$IMAGE" \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus \
  --storage.tsdb.retention.time=15d \
  --storage.tsdb.retention.size=5GB \
  --web.listen-address=127.0.0.1:9090
