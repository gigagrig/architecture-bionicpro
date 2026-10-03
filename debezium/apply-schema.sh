#!/usr/bin/env bash
set -o pipefail
logs_dir=./log
schema_file=/schema/cdc.sql
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo 'Apply the idempotent ClickHouse CDC schema.'
      echo 'Usage: apply-schema.sh [--input FILE] [--logs-dir DIR] [-h]'
      echo 'Example: apply-schema.sh --input /schema/cdc.sql --logs-dir /tmp/log'
      echo 'Environment: CLICKHOUSE_PASSWORD, CLICKHOUSE_HOST (default clickhouse).'
      echo 'Writes a client log; exits 0 success, 1 failure, 2 arguments.'
      exit 0 ;;
    --logs-dir|--input)
      [[ $# -ge 2 ]] || { echo 'Missing value'; exit 2; }
      if [[ $1 == --input ]]; then schema_file=$2; else logs_dir=$2; fi; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
for tool in clickhouse-client mkdir date; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool"; exit 1; }
done
echo "Start schema setup: $schema_file"
[[ -f $schema_file ]] || { echo "Missing input: $schema_file"; exit 1; }
mkdir -p -- "$logs_dir" || exit 1
echo "Log directory ready: $logs_dir"
log_file="$logs_dir/clickhouse-client_$(date +%Y%m%d_%H%M%S)_cdc_$$.log"
echo "Apply $schema_file; log: $log_file"
if clickhouse-client --host "${CLICKHOUSE_HOST:-clickhouse}" --user etl \
    --password "$CLICKHOUSE_PASSWORD" --multiquery <"$schema_file" >"$log_file" 2>&1; then
  echo "Schema setup complete: $schema_file"
else
  echo "Schema setup failed: $schema_file; see $log_file"
  exit 1
fi
