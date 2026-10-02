"""Run a real daily DAG and verify self-only reports through HTTPS BFF + Keycloak."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx
import pytest
import ssl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bionicpro-auth/tests"))
from test_live import env, browser, admin, user, login_with_setup, ORIGIN

pytestmark = pytest.mark.skipif(os.getenv("BIONICPRO_E2E") != "1", reason="Set BIONICPRO_E2E=1")
ROOT = Path(__file__).resolve().parents[2]


def sql(service, database, query):
    subprocess.run(["docker", "compose", "exec", "-T", service, "psql", "-U", database,
                    "-d", database, "-v", "ON_ERROR_STOP=1", "-q"], input=query,
                   text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)


def process_day(day):
    # Wait for a post-write heartbeat from BOTH sources. The single ordered
    # source topic then contains all earlier commits before publication starts.
    after = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
    deadline = time.monotonic() + 60
    while True:
        query = "SELECT count() FROM reporting.cdc_state WHERE table_name='cdc_control' AND NOT deleted " \
            f"AND parseDateTime64BestEffortOrNull(JSONExtractString(payload,'touched_at'),6,'UTC') > toDateTime64('{after}',6,'UTC')"
        result = subprocess.run(['docker','compose','exec','-T','clickhouse','sh','-c',
            'clickhouse-client --user etl --password "$CLICKHOUSE_PASSWORD"'],
            input=query, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
        if result.stdout.strip() == '2':
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('Both CDC heartbeats did not arrive')
        time.sleep(1)
    logs = ROOT / "log"
    logs.mkdir(exist_ok=True)
    log = logs / f"airflow_{time.strftime('%Y%m%d_%H%M%S')}_{day}_{os.getpid()}_{secrets.token_hex(3)}.log"
    print(f"DAG date={day}; log={log}")
    with log.open("w") as output:
        # A forced day preserves the earlier daily test contract. The publication
        # DAG is now scheduled every minute and reads ClickHouse exclusively.
        trigger = (datetime.fromisoformat(day) + timedelta(days=1, hours=12)).isoformat() + "+00:00"
        subprocess.run(["docker", "compose", "exec", "-T", "airflow-scheduler", "airflow",
                        "dags", "test", "bionicpro_reports", trigger, "--conf", '{"day":"' + day + '"}'], stdout=output,
                       stderr=subprocess.STDOUT, check=True, timeout=180, cwd=ROOT)


def fetch_report(client, period, endpoint="/api/reports"):
    link = client.get(endpoint, params=period)
    assert link.status_code == 200, link.text
    return client.get(link.json()["download_url"])


def test_reports_are_private_complete_and_repeatable(browser, admin, user):
    other = "reports-other-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    created = admin("POST", "/users", json={"username": other, "enabled": True,
        "email":other + "@example.com", "firstName":"Other", "lastName":"Test",
        "requiredActions":["CONFIGURE_TOTP"],
        "credentials":[{"type":"password", "value":password, "temporary":False}]})
    other_id = created.headers["location"].rsplit("/", 1)[-1]
    owner_id = admin("GET", "/users", params={"username":user[0], "exact":"true"}).json()[0]["id"]
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    identifiers = [owner_id, other_id]
    devices = ["e2e-" + secrets.token_hex(8) for _ in range(3)]
    period = {"from":start.date().isoformat(), "to":end.date().isoformat()}
    try:
        # IDs come from our own freshly created Keycloak users, not request parameters.
        for subject, owned in ((owner_id, devices[:2]), (other_id, devices[2:])):
            sql("crm_db", "crm", f"INSERT INTO customers VALUES ('{subject}','{subject}');")
            for device in owned:
                sql("crm_db", "crm", f"INSERT INTO prostheses VALUES ('{device}','E2E'); "
                    f"INSERT INTO ownership VALUES ('{device}','{subject}','2026-01-01',NULL);")
        for device, moves in ((devices[0], 7), (devices[2], 19)):
            sql("telemetry_db", "telemetry", f"INSERT INTO telemetry VALUES ('{device}','{device}',"
                f"'{start.isoformat()}',{moves},80.0,90.0,FALSE);")
        for service, database in (("crm_db", "crm"), ("telemetry_db", "telemetry")):
            sql(service, database, f"INSERT INTO export_checkpoint VALUES (TRUE,'{end.isoformat()}') "
                "ON CONFLICT (id) DO UPDATE SET closed_through=GREATEST(export_checkpoint.closed_through,EXCLUDED.closed_through);")
        assert browser.get("/api/reports", params=period).status_code == 401
        assert browser.get("/reports", params=period).status_code == 401
        process_day(start.date().isoformat())
        login_with_setup(browser, user[0], user[1])
        first = fetch_report(browser, period)
        assert first.status_code == 200, first.text
        rows = first.json()["rows"]
        assert {r["prosthesis_id"] for r in rows} == set(devices[:2])
        active = next(r for r in rows if r["prosthesis_id"] == devices[0])
        assert (active["samples"], active["movements"], active["avg_response_ms"]) == (1, 7, 80.0)
        spare = next(r for r in rows if r["prosthesis_id"] == devices[1])
        assert spare["samples"] == 0 and spare["avg_response_ms"] is None
        assert browser.get("/api/reports", params={**period, "user_id":other_id}).status_code == 400
        assert browser.get("/api/reports", params={**period, "subject":other_id}).status_code == 400
        assert fetch_report(browser, period, "/reports").json()["rows"] == rows
        unavailable = browser.get("/api/reports", params={"from":start.date().isoformat(), "to":(end + timedelta(days=1)).date().isoformat()})
        assert unavailable.status_code == 409
        assert end.date().isoformat() in unavailable.json()["missing_days"]
        gap = browser.get("/api/reports", params={"from":"2026-01-01", "to":"2026-01-03"})
        assert gap.status_code == 409
        process_day(start.date().isoformat())
        repeat = fetch_report(browser, period)
        assert repeat.status_code == 200 and repeat.json()["rows"] == rows
        assert repeat.json()["periods"][0]["batch_id"] != first.json()["periods"][0]["batch_id"]
        context = ssl.create_default_context(cafile=str(ROOT / ".local/tls/localhost.crt"))
        with httpx.Client(base_url=ORIGIN, verify=context, follow_redirects=False, timeout=20) as other_browser:
            login_with_setup(other_browser, other, password)
            response = fetch_report(other_browser, period)
            assert response.status_code == 200
            assert len(response.json()["rows"]) == 1
            assert response.json()["rows"][0]["prosthesis_id"] == devices[2]
            assert response.json()["rows"][0]["movements"] == 19
    finally:
        for device in devices:
            sql("telemetry_db", "telemetry", f"DELETE FROM telemetry WHERE prosthesis_id='{device}';")
            sql("crm_db", "crm", f"DELETE FROM ownership WHERE prosthesis_id='{device}'; DELETE FROM prostheses WHERE prosthesis_id='{device}';")
        for subject in identifiers:
            sql("crm_db", "crm", f"DELETE FROM customers WHERE customer_id='{subject}';")
        admin("DELETE", "/users/" + other_id)
