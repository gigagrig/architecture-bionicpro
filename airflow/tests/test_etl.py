"""ETL invariants: attribution by event time and refusing incomplete publication."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pendulum
import pytest
from report_etl import transform, interval, publish, publish_catalog, dag

START = datetime(2026, 9, 28, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def test_transfer_multiple_devices_and_empty_day():
    split = START + timedelta(hours=12)
    owners = [("hand", "a", "model", START, split), ("hand", "b", "model", split, None),
              ("spare", "a", "model", START, None)]
    events = [("hand", START, 2, 80.0, 95.0, False), ("hand", split, 3, 100.0, 85.0, True)]
    rows = {(r["subject"], r["prosthesis_id"]): r for r in transform(owners, events, START, END)}
    assert rows[("a", "hand")]["movements"] == 2
    assert rows[("b", "hand")]["movements"] == 3
    assert rows[("b", "hand")]["errors"] == 1
    assert rows[("a", "spare")]["samples"] == 0
    assert rows[("a", "spare")]["avg_response_ms"] is None


def test_orphan_telemetry_fails_instead_of_silent_loss():
    with pytest.raises(ValueError, match="ownership"):
        transform([], [("unknown", START, 1, 80.0, 99.0, False)], START, END)


def test_overlapping_ownership_fails():
    owners = [("hand", s, "model", START, None) for s in ("a", "b")]
    with pytest.raises(ValueError, match="ambiguous"):
        transform(owners, [("hand", START, 1, 80.0, 99.0, False)], START, END)


def test_interval_must_be_complete_utc_day():
    start = pendulum.datetime(2026, 9, 28, tz="UTC")
    assert interval(dict(data_interval_start=start, data_interval_end=start.add(days=1))) == (start, start.add(days=1))
    with pytest.raises(ValueError):
        interval(dict(data_interval_start=start.add(hours=1), data_interval_end=start.add(days=1)))
    with pytest.raises(ValueError):
        interval(dict(data_interval_start=pendulum.now("UTC").start_of("day"),
                      data_interval_end=pendulum.now("UTC").start_of("day").add(days=1)))


def test_publish_is_last_and_requires_all_rows(monkeypatch):
    import report_etl
    metadata = dict(day="2026-09-28", batch_id="00000000-0000-0000-0000-000000000001", row_count=2)
    ti = Mock()
    ti.xcom_pull.return_value = metadata
    writes = []
    monkeypatch.setattr(report_etl, "ch", lambda sql, rows=None, params=None: writes.append(sql) or "1\n")
    with pytest.raises(ValueError, match="Incomplete"):
        publish(ti=ti)
    assert len(writes) == 1
    assert "INSERT" not in writes[0]


def test_catalog_is_after_commit_and_contains_only_versions(monkeypatch):
    import json
    import report_etl
    periods = [dict(day="2026-09-28", batch_id="00000000-0000-0000-0000-000000000001", published_at="2026-09-29 00:00:00.000")]
    monkeypatch.setattr(report_etl, "ch", lambda query: json.dumps({"data":periods}))
    for key in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.setenv(key, "test")
    client = Mock()
    client.get_object.return_value = {"Body":Mock(), "ETag":"old-version"}
    monkeypatch.setattr(report_etl.boto3, "client", lambda *args, **kwargs: client)
    publish_catalog()
    args = client.put_object.call_args.kwargs
    assert args["Key"] == "catalog/current.json" and args["CacheControl"] == "no-store"
    assert args["IfMatch"] == "old-version"
    assert json.loads(args["Body"]) == {"schema":1, "periods":periods}
    assert dag.get_task("publish_catalog").upstream_task_ids == {"publish"}


def test_catalog_failure_propagates_for_retry(monkeypatch):
    import report_etl
    monkeypatch.setattr(report_etl, "ch", lambda query: '{"data":[]}')
    for key in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.setenv(key, "test")
    client = Mock()
    client.get_object.return_value = {"Body":Mock(), "ETag":"old-version"}
    client.put_object.side_effect = RuntimeError("S3 unavailable")
    monkeypatch.setattr(report_etl.boto3, "client", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match="S3"):
        publish_catalog()


def test_catalog_bootstrap_and_conflict(monkeypatch):
    import report_etl
    from botocore.exceptions import ClientError
    monkeypatch.setattr(report_etl, "ch", lambda query: '{"data":[]}')
    for key in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.setenv(key, "test")
    client = Mock()
    client.get_object.side_effect = ClientError({"Error":{"Code":"NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(report_etl.boto3, "client", lambda *args, **kwargs: client)
    publish_catalog()
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    client.put_object.side_effect = ClientError({"Error":{"Code":"PreconditionFailed"}}, "PutObject")
    with pytest.raises(ClientError):
        publish_catalog()
