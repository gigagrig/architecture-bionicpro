#!/usr/bin/env bash
set -o pipefail
logs_dir=./log
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo 'Prepare a PostgreSQL CDC role, publication and replica identities.'
      echo 'Usage: prepare-source.sh [--logs-dir DIR] [-h]'
      echo 'Environment: PGHOST, PGUSER, PGDATABASE, PGPASSWORD, CDC_PASSWORD, CDC_SOURCE=crm|telemetry.'
      echo 'Example: CDC_SOURCE=crm prepare-source.sh --logs-dir /tmp/log'
      echo 'Writes a psql log in DIR. Exit: 0 success, 1 failure, 2 arguments.'
      exit 0 ;;
    --logs-dir) [[ $# -ge 2 ]] || { echo 'Missing DIR'; exit 2; }; logs_dir=$2; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
for tool in psql cat mkdir date; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool"; exit 1; }
done
[[ $CDC_SOURCE == crm || $CDC_SOURCE == telemetry ]] || { echo 'CDC_SOURCE must be crm or telemetry'; exit 2; }
config_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
echo "Start source setup: $CDC_SOURCE; SQL directory: $config_dir"
mkdir -p -- "$logs_dir" || exit 1
echo "Log directory ready: $logs_dir"
log_file="$logs_dir/psql_$(date +%Y%m%d_%H%M%S)_${CDC_SOURCE}_$$.log"
echo "Apply source-common.sql and source-$CDC_SOURCE.sql; log: $log_file"
if cat "$config_dir/source-common.sql" "$config_dir/source-$CDC_SOURCE.sql" | psql -X -v ON_ERROR_STOP=1 >"$log_file" 2>&1; then
  echo "Source setup complete: $CDC_SOURCE"
else
  echo "Source setup failed: $config_dir/source-$CDC_SOURCE.sql; see $log_file"
  exit 1
fi
