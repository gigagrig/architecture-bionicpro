"""Publish immutable reports from a CDC materialized view; never query OLTP."""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pendulum
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from airflow import DAG
from airflow.operators.python import PythonOperator


def ch(sql, rows=None, params=None):
    body = sql
    if rows is not None:
        body += " FORMAT JSONEachRow\n" + "\n".join(json.dumps(row) for row in rows)
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        result = client.post(os.environ["CLICKHOUSE_URL"], content=body.encode(),
                             params={"wait_end_of_query": "1", "output_format_json_quote_64bit_integers": "0", **(params or {})},
                             auth=("etl", os.environ["CLICKHOUSE_ETL_PASSWORD"]))
        # Do not put server responses (potentially containing patient data) in logs.
        if result.status_code != 200:
            raise RuntimeError("ClickHouse operation failed")
        return result.text


def refresh_views():
    for name in ("cdc_state_mv", "cdc_report_current_mv"):
        ch(f"SYSTEM REFRESH VIEW reporting.{name}")
        ch(f"SYSTEM WAIT VIEW reporting.{name}")


def commit(metadata, rows):
    """Publish only fully inserted immutable batches; an empty day is valid."""
    for offset in range(0, len(rows), 10000):
        ch("INSERT INTO reporting.cdc_report_mart", rows[offset:offset + 10000])
    count = int(ch("SELECT count() FROM reporting.cdc_report_mart WHERE batch_id={batch:UUID}",
                   params={"param_batch": metadata["batch_id"]}).strip())
    if count != metadata["row_count"]:
        raise ValueError("Incomplete mart; publication refused")
    metadata.update(version=time.time_ns(), published_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    ch("INSERT INTO reporting.cdc_report_periods", [metadata])


def sync_reports(**context):
    """Read only an atomic OLAP snapshot, publish changed days, then repair S3."""
    import hashlib
    refresh_views()
    previous = json.loads(ch("SELECT day, batch_id, content_hash FROM reporting.cdc_report_periods FINAL FORMAT JSON"))["data"]
    by_day = {p["day"]: p for p in previous}
    run = context.get("dag_run")
    requested = (run.conf or {}).get("day") if run else None
    days = set(by_day)
    if requested:
        day = datetime.strptime(requested, "%Y-%m-%d").date()
        if day.isoformat() != requested or day >= datetime.now(timezone.utc).date():
            raise ValueError("day must be a completed UTC day YYYY-MM-DD")
        days.add(requested)
    else:
        days.add((datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    # Both report rows and readiness rows come from the SAME atomic MV target.
    data = json.loads(ch("""SELECT subject, day, prosthesis_id, model, samples, movements, errors,
        avg_response_ms, max_response_ms, min_battery_pct, row_kind, ready
        FROM reporting.cdc_report_current WHERE day IN {days:Array(Date)}
        ORDER BY day, subject, prosthesis_id, row_kind FORMAT JSON""",
        params={"param_days": "['" + "','".join(sorted(days)) + "']"}))["data"]
    grouped, ready = defaultdict(list), set()
    for row in data:
        kind, available = row.pop("row_kind"), row.pop("ready")
        if kind == "period":
            if int(available) == 1:
                ready.add(row["day"])
        else:
            grouped[row["day"]].append(row)
    if requested and requested not in ready:
        raise ValueError("CDC period is not ready: checkpoints, heartbeat or ownership")
    changed = 0
    for day in sorted(days & ready):
        rows = grouped[day]
        fingerprint = hashlib.sha256(json.dumps([day, rows], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if day != requested and by_day.get(day, {}).get("content_hash") == fingerprint:
            continue
        batch = str(uuid4())
        for row in rows:
            row["batch_id"] = batch
        commit(dict(day=day, batch_id=batch, row_count=len(rows), content_hash=fingerprint), rows)
        changed += 1
    # Runs even after a prior publication had failed to update S3; unchanged data
    # does not cause new ClickHouse batches or invalidate cached report objects.
    publish_catalog()
    return {"published_days": changed, "pending_days": len(days - ready)}


def publish_catalog():
    """Publish current versions atomically; retry repairs a failed S3 publication."""
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="us-east-1", config=Config(signature_version="s3v4", connect_timeout=5,
        read_timeout=15, retries={"max_attempts":2}, s3={"addressing_style":"path"}))
    # Read the ETag BEFORE the ClickHouse snapshot. Conditional PUT prevents a
    # slower concurrent manual publisher replacing a newer catalog.
    try:
        previous = client.get_object(Bucket=os.environ["S3_BUCKET"], Key="catalog/cdc-current.json")
        previous["Body"].close()
        condition = {"IfMatch": previous["ETag"]}
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchKey":
            raise
        condition = {"IfNoneMatch": "*"}
    periods = json.loads(ch("SELECT day, batch_id, published_at FROM reporting.cdc_report_periods FINAL ORDER BY day FORMAT JSON"))["data"]
    client.put_object(Bucket=os.environ["S3_BUCKET"], Key="catalog/cdc-current.json",
        Body=json.dumps({"schema":1, "periods":periods}).encode(),
        ContentType="application/json", CacheControl="no-store", **condition)


with DAG("bionicpro_reports", description="CDC report publication from ClickHouse only",
         start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), schedule="* * * * *",
         catchup=False, max_active_runs=1, is_paused_upon_creation=False,
         default_args={"retries": 2, "retry_delay": timedelta(minutes=1),
                       "execution_timeout": timedelta(minutes=10)},
         tags=["bionicpro", "reports", "cdc"]) as dag:
    PythonOperator(task_id="publish_cdc", python_callable=sync_reports)
