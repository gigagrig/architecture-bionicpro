"""CDC publication: complete snapshots, unchanged content, retries and private catalogs."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import report_etl
from report_etl import commit, publish_catalog, sync_reports, dag

DAY = '2026-09-28'
ROW = dict(subject='owner', day=DAY, prosthesis_id='hand', model='demo',
    samples=1, movements=7, errors=0, avg_response_ms=80.0, max_response_ms=80.0, min_battery_pct=90.0)


def mock_snapshot(monkeypatch, previous=None, ready=1, rows=None):
    calls = []
    def query(sql, rows=None, params=None):
        calls.append(sql)
        if 'content_hash FROM reporting.cdc_report_periods' in sql:
            return json.dumps({'data': previous or []})
        if 'FROM reporting.cdc_report_current' in sql:
            data = [dict(row, row_kind='report', ready=1) for row in records]
            data.append(dict(ROW, row_kind='period', ready=ready))
            return json.dumps({'data': data})
        raise AssertionError(sql)
    records = [ROW] if rows is None else rows
    monkeypatch.setattr(report_etl, 'ch', query)
    monkeypatch.setattr(report_etl, 'refresh_views', Mock())
    monkeypatch.setattr(report_etl, 'commit', Mock())
    monkeypatch.setattr(report_etl, 'publish_catalog', Mock())
    return calls


def test_incomplete_snapshot_never_publishes(monkeypatch):
    mock_snapshot(monkeypatch, ready=0)
    with pytest.raises(ValueError, match='not ready'):
        sync_reports(dag_run=SimpleNamespace(conf={'day': DAY}))
    report_etl.commit.assert_not_called()
    report_etl.publish_catalog.assert_not_called()


def test_new_snapshot_pins_uuid_and_metadata_without_reading_oltp(monkeypatch):
    calls = mock_snapshot(monkeypatch)
    result = sync_reports(dag_run=SimpleNamespace(conf={'day': DAY}))
    assert result['published_days'] == 1
    metadata, rows = report_etl.commit.call_args.args
    assert metadata['day'] == DAY and metadata['row_count'] == 1
    assert rows[0]['batch_id'] == metadata['batch_id']
    assert len(metadata['content_hash']) == 64
    assert all('reporting.cdc_' in sql for sql in calls)
    report_etl.publish_catalog.assert_called_once()
    assert dag.task_ids == ['publish_cdc'] and dag.max_active_runs == 1


def test_unchanged_data_preserves_version_and_repairs_failed_s3_publication(monkeypatch):
    digest = hashlib.sha256(json.dumps([DAY, [ROW]], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    mock_snapshot(monkeypatch, previous=[dict(day=DAY, content_hash=digest)])
    result = sync_reports()
    assert result['published_days'] == 0
    report_etl.commit.assert_not_called()
    report_etl.publish_catalog.assert_called_once()


def test_empty_closed_day_publishes_zero_rows(monkeypatch):
    mock_snapshot(monkeypatch, rows=[])
    sync_reports(dag_run=SimpleNamespace(conf={'day': DAY}))
    metadata, rows = report_etl.commit.call_args.args
    assert rows == [] and metadata['row_count'] == 0


def test_partial_batch_is_never_marked_ready(monkeypatch):
    writes = []
    monkeypatch.setattr(report_etl, 'ch', lambda sql, rows=None, params=None: writes.append(sql) or '1\n')
    with pytest.raises(ValueError, match='Incomplete'):
        commit(dict(batch_id='00000000-0000-0000-0000-000000000001', row_count=2), [ROW, ROW])
    assert not any('cdc_report_periods' in sql for sql in writes)


def catalog_client(monkeypatch):
    for key in ('S3_ENDPOINT', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_BUCKET'):
        monkeypatch.setenv(key, 'test')
    client = Mock()
    client.get_object.return_value = {'Body': Mock(), 'ETag': 'old-version'}
    monkeypatch.setattr(report_etl.boto3, 'client', lambda *args, **kwargs: client)
    return client


def test_catalog_contains_only_cdc_versions_and_uses_conditional_put(monkeypatch):
    periods = [dict(day=DAY, batch_id='00000000-0000-0000-0000-000000000001', published_at='2026-09-29 00:00:00.000')]
    def query(sql):
        assert 'cdc_report_periods FINAL' in sql
        return json.dumps({'data': periods})
    monkeypatch.setattr(report_etl, 'ch', query)
    client = catalog_client(monkeypatch)
    publish_catalog()
    args = client.put_object.call_args.kwargs
    assert args['Key'] == 'catalog/cdc-current.json' and args['CacheControl'] == 'no-store'
    assert args['IfMatch'] == 'old-version'
    assert json.loads(args['Body']) == {'schema': 1, 'periods': periods}


def test_catalog_bootstrap_conflict_and_failure_are_retried(monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setattr(report_etl, 'ch', lambda sql: '{"data":[]}')
    client = catalog_client(monkeypatch)
    client.get_object.side_effect = ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
    publish_catalog()
    assert client.put_object.call_args.kwargs['IfNoneMatch'] == '*'
    client.put_object.side_effect = ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
    with pytest.raises(ClientError):
        publish_catalog()
