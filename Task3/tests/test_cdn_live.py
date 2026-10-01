"""Real S3/CDN cache hits, publication invalidation and cross-user denial over TLS."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Task2/tests"))
from test_reports_live import sql, process_day
from test_live import env, browser, admin, user, login_with_setup, ORIGIN, SID

pytestmark = pytest.mark.skipif(os.getenv("BIONICPRO_E2E") != "1", reason="Set BIONICPRO_E2E=1")


def query_count():
    query = "SYSTEM FLUSH LOGS; SELECT count() FROM system.query_log WHERE type='QueryFinish' AND user='reports_api';"
    result = subprocess.run(["docker", "compose", "exec", "-T", "clickhouse", "sh", "-c",
        'clickhouse-client --user etl --password "$CLICKHOUSE_PASSWORD" --multiquery'],
        input=query, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    return int(result.stdout.strip())


def test_s3_cdn_cache_is_private_and_versions_refresh(browser, admin, user):
    other = "cdn-other-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    created = admin("POST", "/users", json={"username":other, "enabled":True,
        "email":other+"@example.com", "firstName":"CDN", "lastName":"Check",
        "requiredActions":["CONFIGURE_TOTP"], "credentials":[{"type":"password", "value":password, "temporary":False}]})
    other_id = created.headers["location"].rsplit("/",1)[-1]
    owner_id = admin("GET", "/users", params={"username":user[0], "exact":"true"}).json()[0]["id"]
    devices = ["cdn-"+secrets.token_hex(8) for _ in range(2)]
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end-timedelta(days=1)
    period = {"from":start.date().isoformat(), "to":end.date().isoformat()}
    context = ssl.create_default_context(cafile=str(ROOT/".local/tls/localhost.crt"))
    try:
        for subject, device in zip((owner_id,other_id), devices):
            sql("crm_db","crm",f"INSERT INTO customers VALUES ('{subject}','{subject}'); "
                f"INSERT INTO prostheses VALUES ('{device}','CDN'); INSERT INTO ownership VALUES ('{device}','{subject}','2026-01-01',NULL);")
            sql("telemetry_db","telemetry",f"INSERT INTO telemetry VALUES ('{device}','{device}','{start.isoformat()}',7,80,90,FALSE);")
        for service, db in (("crm_db","crm"),("telemetry_db","telemetry")):
            sql(service,db,f"INSERT INTO export_checkpoint VALUES (TRUE,'{end.isoformat()}') "
                "ON CONFLICT (id) DO UPDATE SET closed_through=GREATEST(export_checkpoint.closed_through,EXCLUDED.closed_through);")
        process_day(start.date().isoformat())
        login_with_setup(browser,*user)
        before = query_count()
        metadata = browser.get("/api/reports",params=period)
        assert metadata.status_code == 200, metadata.text
        assert "rows" not in metadata.json() and "minio" not in metadata.text
        link = metadata.json()["download_url"]
        assert link.startswith("/cdn/reports/v1/") and owner_id not in link
        assert query_count() == before+1
        sid = browser.cookies.get(SID)
        first = browser.get(link)
        assert first.status_code == 200, first.text
        assert first.headers["X-Report-Cache"] == "MISS"
        assert first.headers["Cache-Control"] == "private, no-store"
        assert "X-Report-Origin" not in first.headers
        assert browser.cookies.get(SID) != sid
        assert [r["prosthesis_id"] for r in first.json()["rows"]] == devices[:1]
        second = browser.get(link)
        assert second.headers["X-Report-Cache"] == "HIT" and second.content == first.content
        repeat = browser.get("/reports",params=period)
        assert urlsplit(repeat.json()["download_url"]).path == urlsplit(link).path
        assert query_count() == before+1
        with httpx.Client(base_url=ORIGIN, verify=context, timeout=30) as other_browser:
            assert other_browser.get(link).status_code == 401
            login_with_setup(other_browser,other,password)
            assert other_browser.get(link).status_code == 403
            other_meta = other_browser.get("/api/reports",params=period)
            assert other_meta.status_code == 200
            other_report = other_browser.get(other_meta.json()["download_url"])
            assert [r["prosthesis_id"] for r in other_report.json()["rows"]] == devices[1:]
        assert browser.get(link.replace("signature=","signature=0")).status_code == 403
        assert browser.get(link+"&expires=0").status_code == 403
        assert browser.get("/internal/report-authorize",headers={"X-Original-URI":link}).status_code == 404
        # Republish an actually changed source: same period -> new object and CDN MISS.
        sql("telemetry_db","telemetry",f"UPDATE telemetry SET movements=17 WHERE prosthesis_id='{devices[0]}';")
        process_day(start.date().isoformat())
        before_update = query_count()
        fresh = browser.get("/api/reports",params=period)
        fresh_link = fresh.json()["download_url"]
        assert urlsplit(fresh_link).path != urlsplit(link).path
        assert query_count() == before_update+1
        updated = browser.get(fresh_link)
        assert updated.status_code == 200 and updated.headers["X-Report-Cache"] == "MISS"
        assert updated.json()["rows"][0]["movements"] == 17
        assert browser.get(fresh_link).headers["X-Report-Cache"] == "HIT"
        assert browser.get(link).json()["rows"][0]["movements"] == 7  # Immutable snapshot.
        session = browser.get("/auth/session").json()
        assert browser.post("/auth/logout",headers={"Origin":ORIGIN,"X-CSRF-Token":session["csrf"]}).status_code == 200
        assert browser.get(fresh_link).status_code == 401  # Warm cache cannot bypass logout.
        print("Verified: S3 reuse 0 OLAP queries; CDN MISS/HIT; owner-only access; fresh ETL path; logout denial")
    finally:
        for device in devices:
            sql("telemetry_db","telemetry",f"DELETE FROM telemetry WHERE prosthesis_id='{device}';")
            sql("crm_db","crm",f"DELETE FROM ownership WHERE prosthesis_id='{device}'; DELETE FROM prostheses WHERE prosthesis_id='{device}';")
        for subject in (owner_id,other_id):
            sql("crm_db","crm",f"DELETE FROM customers WHERE customer_id='{subject}';")
        admin("DELETE","/users/"+other_id)
