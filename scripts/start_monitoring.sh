#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host_gateway="$(ip route show default | awk 'NR == 1 {print $3}')"

if [[ -z "${host_gateway}" ]]; then
  echo "Unable to detect the WSL default gateway." >&2
  exit 1
fi

export HOST_GATEWAY_IP="${host_gateway}"
docker compose --project-directory "${project_dir}" \
  -f "${project_dir}/docker-compose.monitoring.yml" up -d

echo "Monitoring stack started; Prometheus targets host.docker.internal -> ${host_gateway}."
