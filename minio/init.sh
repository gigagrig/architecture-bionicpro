#!/usr/bin/env bash
set -o pipefail

usage() {
    cat <<'HELP'
Create a private BionicPRO bucket and restricted API/ETL users in local MinIO.
Usage: init.sh [-h] [--logs-dir DIRECTORY]
  -h                 Show this help.
  --logs-dir DIR     mc invocation logs (default: ./log).
Requires mc, S3_ROOT_PASSWORD, S3_REPORTS_SECRET and S3_ETL_SECRET.
Example: docker compose run --rm minio-init --logs-dir /tmp/log
Changes: MinIO bucket/policies/users; logs and /tmp/mc/config.json (contains secrets).
Exit codes: 0 success, 1 setup failure, 2 argument error. Safe to repeat.
HELP
}

run_mc() {
    local stage="$1" log_path
    shift
    log_path="$logs_dir/mc_$(date +%Y%m%d_%H%M%S)_${stage}_$$_${step}.log"
    step=$((step + 1))
    printf 'MinIO: %s; invocation log: %s\n' "$stage" "$log_path"
    if ! mc --config-dir /tmp/mc "$@" >"$log_path" 2>&1; then
        printf 'MinIO setup failed: %s; see %s\n' "$stage" "$log_path"
        return 1
    fi
}

main() {
    local logs_dir=./log step=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help) usage; return 0 ;;
            --logs-dir)
                if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing --logs-dir value\n'; return 2; fi
                logs_dir="$2"; shift 2 ;;
            *) printf 'Unknown argument: %s\n' "$1"; usage; return 2 ;;
        esac
    done
    for executable in mc date mkdir; do
        if ! command -v "$executable" >/dev/null; then printf 'Missing executable: %s\n' "$executable"; return 1; fi
    done
    if [[ -z "$S3_ROOT_PASSWORD" || -z "$S3_REPORTS_SECRET" || -z "$S3_ETL_SECRET" ]]; then
        printf 'Missing required MinIO secrets\n'; return 1
    fi
    umask 077
    printf 'Preparing MinIO at http://minio:9000; policies: /init; logs: %s\n' "$logs_dir"
    if ! mkdir -p "$logs_dir" 2>&1; then printf 'Cannot create %s\n' "$logs_dir"; return 1; fi
    printf 'Created/checked log directory: %s\n' "$logs_dir"
    run_mc alias alias set local http://minio:9000 bionicpro-root "$S3_ROOT_PASSWORD" || return 1
    printf 'Created/updated mc configuration: /tmp/mc/config.json\n'
    run_mc bucket mb --ignore-existing local/bionicpro-reports || return 1
    run_mc private anonymous set none local/bionicpro-reports || return 1
    run_mc reports-policy admin policy create local reports-api /init/reports-policy.json || return 1
    run_mc etl-policy admin policy create local report-etl /init/etl-policy.json || return 1
    run_mc reports-user admin user add local reports-api "$S3_REPORTS_SECRET" || return 1
    run_mc etl-user admin user add local report-etl "$S3_ETL_SECRET" || return 1
    run_mc reports-attach admin policy attach local reports-api --user reports-api || return 1
    run_mc etl-attach admin policy attach local report-etl --user report-etl || return 1
    printf 'MinIO setup complete: private bucket, 2 restricted users and 2 policies\n'
}

main "$@"
