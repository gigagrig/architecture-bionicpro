#!/usr/bin/env bash
set -o pipefail
logs_dir=./log
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo 'Create persistent single-partition CDC topics and compacted Connect topics.'
      echo 'Usage: topics.sh [--logs-dir DIR] [-h]'
      echo 'Example: topics.sh --logs-dir /tmp/log'
      echo 'Environment: KAFKA_BIN (default /opt/kafka/bin), KAFKA_BROKER (default kafka:9092).'
      echo 'Writes one log per topic; exits 0 success, 1 failure, 2 arguments.'
      exit 0 ;;
    --logs-dir) [[ $# -ge 2 ]] || { echo 'Missing DIR'; exit 2; }; logs_dir=$2; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
kafka_bin=${KAFKA_BIN:-/opt/kafka/bin}
broker=${KAFKA_BROKER:-kafka:9092}
for tool in "$kafka_bin/kafka-topics.sh" mkdir date; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool"; exit 1; }
done
echo "Start topic setup: $broker; executables: $kafka_bin"
mkdir -p -- "$logs_dir" || exit 1
echo "Log directory ready: $logs_dir"
failures=0
for topic in connect-configs connect-offsets connect-status crm.events telemetry.events; do
  cleanup=compact
  [[ $topic == *.events ]] && cleanup=delete
  log_file="$logs_dir/kafka-topics_$(date +%Y%m%d_%H%M%S)_${topic}_$$.log"
  echo "Create or preserve $topic; log: $log_file"
  "$kafka_bin/kafka-topics.sh" --bootstrap-server "$broker" --create --if-not-exists \
    --topic "$topic" --partitions 1 --replication-factor 1 --config "cleanup.policy=$cleanup" \
    >"$log_file" 2>&1 || failures=$((failures + 1))
done
echo "Topic setup complete: 5 topics, $failures errors"
[[ $failures == 0 ]]
