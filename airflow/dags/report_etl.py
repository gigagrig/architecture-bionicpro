"""Build a daily report from independent CRM and telemetry snapshots."""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pendulum
import psycopg
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
                             params={"wait_end_of_query": "1", **(params or {})},
                             auth=("etl", os.environ["CLICKHOUSE_ETL_PASSWORD"]))
        # Do not put server responses (potentially containing patient data) in logs.
        if result.status_code != 200:
            raise RuntimeError("ClickHouse operation failed")
        return result.text


def interval(context):
    start, end = context["data_interval_start"], context["data_interval_end"]
    start, end = start.in_timezone("UTC"), end.in_timezone("UTC")
    if start.hour or start.minute or start.second or start.microsecond or end - start != timedelta(days=1):
        raise ValueError("Reports require a complete UTC day")
    if end > datetime.now(timezone.utc):
        raise ValueError("The interval has not ended")
    return start, end


def snapshot(dsn, end):
    conn = psycopg.connect(dsn, connect_timeout=10)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        checkpoint = conn.execute("SELECT closed_through FROM export_checkpoint WHERE id").fetchone()
        if not checkpoint or checkpoint[0] < end:
            raise ValueError("Source has not closed the requested interval")
        return conn
    except Exception:
        conn.close()
        raise


def transform(owners, events, start, end):
    """Resolve ownership at event time, including transfers and zero-activity days."""
    lookup = defaultdict(list)
    totals = {}
    for device, subject, model, valid_from, valid_to in owners:
        lookup[device].append((subject, valid_from, valid_to))
        totals.setdefault((subject, device), dict(subject=subject, day=start.date().isoformat(),
            prosthesis_id=device, model=model, samples=0, movements=0, errors=0,
            response_sum=0.0, max_response_ms=None, min_battery_pct=None))
    for device, occurred, moves, response, battery, error in events:
        matches = [subject for subject, low, high in lookup.get(device, [])
                   if low <= occurred and (high is None or occurred < high)]
        if len(matches) != 1:
            raise ValueError("Telemetry ownership is missing or ambiguous")
        row = totals[(matches[0], device)]
        row["samples"] += 1
        row["movements"] += moves
        row["errors"] += int(error)
        row["response_sum"] += response
        row["max_response_ms"] = max(row["max_response_ms"] or 0, response)
        row["min_battery_pct"] = battery if row["min_battery_pct"] is None else min(row["min_battery_pct"], battery)
    for row in totals.values():
        row["avg_response_ms"] = row.pop("response_sum") / row["samples"] if row["samples"] else None
    return list(totals.values())


def build_mart(**context):
    start, end = interval(context)
    with snapshot(os.environ["CRM_DATABASE_URL"], end) as crm:
        owners = crm.execute("""SELECT o.prosthesis_id, c.keycloak_subject, p.model, o.valid_from, o.valid_to
            FROM ownership o JOIN customers c USING(customer_id) JOIN prostheses p USING(prosthesis_id)
            WHERE o.valid_from < %s AND (o.valid_to IS NULL OR o.valid_to > %s)""", (end, start)).fetchall()
    with snapshot(os.environ["TELEMETRY_DATABASE_URL"], end) as telemetry:
        # Named cursor streams raw events without loading the entire day in memory.
        with telemetry.cursor(name="report_events") as cursor:
            cursor.itersize = 10000
            cursor.execute("""SELECT prosthesis_id, occurred_at, movements, response_ms, battery_pct, is_error
                FROM telemetry WHERE occurred_at >= %s AND occurred_at < %s ORDER BY occurred_at""", (start, end))
            rows = transform(owners, cursor, start, end)
    batch = str(uuid4())
    for row in rows:
        row["batch_id"] = batch
    for offset in range(0, len(rows), 10000):
        ch("INSERT INTO reporting.report_mart", rows[offset:offset + 10000])
    # XCom contains only publication metadata, never CRM records or telemetry.
    return dict(day=start.date().isoformat(), batch_id=batch, row_count=len(rows))


def publish(**context):
    metadata = context["ti"].xcom_pull(task_ids="build_mart")
    count = int(ch("SELECT count() FROM reporting.report_mart WHERE batch_id={batch:UUID}",
                   params={"param_batch": metadata["batch_id"]}).strip())
    if count != metadata["row_count"]:
        raise ValueError("Incomplete mart; publication refused")
    metadata.update(version=time.time_ns(), published_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    ch("INSERT INTO reporting.report_periods", [metadata])


def publish_catalog():
    """Publish current versions atomically; retry repairs a failed S3 publication."""
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="us-east-1", config=Config(signature_version="s3v4", connect_timeout=5,
        read_timeout=15, retries={"max_attempts":2}, s3={"addressing_style":"path"}))
    # Read the ETag BEFORE the ClickHouse snapshot. Conditional PUT prevents a
    # slower concurrent manual publisher replacing a newer catalog.
    try:
        previous = client.get_object(Bucket=os.environ["S3_BUCKET"], Key="catalog/current.json")
        previous["Body"].close()
        condition = {"IfMatch": previous["ETag"]}
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchKey":
            raise
        condition = {"IfNoneMatch": "*"}
    periods = json.loads(ch("SELECT day, batch_id, published_at FROM reporting.report_periods FINAL ORDER BY day FORMAT JSON"))["data"]
    client.put_object(Bucket=os.environ["S3_BUCKET"], Key="catalog/current.json",
        Body=json.dumps({"schema":1, "periods":periods}).encode(),
        ContentType="application/json", CacheControl="no-store", **condition)


with DAG("bionicpro_reports", description="Daily CRM + telemetry report mart",
         start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), schedule="@daily",
         catchup=False, max_active_runs=1, is_paused_upon_creation=False,
         default_args={"retries": 2, "retry_delay": timedelta(minutes=1),
                       "execution_timeout": timedelta(hours=2)},
         tags=["bionicpro", "reports"]) as dag:
    build = PythonOperator(task_id="build_mart", python_callable=build_mart)
    commit = PythonOperator(task_id="publish", python_callable=publish)
    catalog = PythonOperator(task_id="publish_catalog", python_callable=publish_catalog)
    build >> commit >> catalog
