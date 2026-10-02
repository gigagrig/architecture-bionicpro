CREATE DATABASE IF NOT EXISTS reporting;

-- The durable journal is written before Kafka offsets are committed. Re-delivery
-- can duplicate messages; all state queries deduplicate source positions.
CREATE TABLE IF NOT EXISTS reporting.cdc_log (
    topic LowCardinality(String),
    partition UInt64,
    offset UInt64,
    raw String,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = MergeTree ORDER BY (topic, partition, offset);

CREATE TABLE IF NOT EXISTS reporting.cdc_kafka (raw String)
ENGINE = Kafka SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.events,telemetry.events',
    kafka_group_name = 'bionicpro-clickhouse-v1',
    kafka_format = 'JSONAsString',
    kafka_num_consumers = 1,
    kafka_thread_per_consumer = 0,
    kafka_skip_broken_messages = 0,
    kafka_flush_interval_ms = 1000,
    kafka_handle_error_mode = 'default';

CREATE MATERIALIZED VIEW IF NOT EXISTS reporting.cdc_ingest TO reporting.cdc_log AS
SELECT _topic AS topic, toUInt64(_partition) AS partition,
       toUInt64(_offset) AS offset, raw FROM reporting.cdc_kafka;

CREATE VIEW IF NOT EXISTS reporting.cdc_events AS
SELECT topic, offset, raw,
    JSONExtractString(raw, 'source', 'table') AS table_name,
    JSONExtractString(raw, 'op') AS op,
    JSONExtractString(raw, 'transaction', 'id') AS tx,
    JSONExtractUInt(raw, 'transaction', 'total_order') AS tx_order,
    JSONExtractUInt(raw, 'source', 'lsn') AS lsn,
    if(op = 'd', JSONExtractRaw(raw, 'before'), JSONExtractRaw(raw, 'after')) AS payload
FROM reporting.cdc_log;

-- Require END and every distinct data-event order, even across consumed blocks.
-- Debezium 3.2 uses different LSN suffixes in BEGIN/data/END IDs. Join the
-- transaction number AND its LSN interval; transaction numbers can wrap.
-- Distinct event orders tolerate connector and KafkaEngine re-delivery.
CREATE VIEW IF NOT EXISTS reporting.cdc_complete_transactions AS
WITH boundaries AS (
    SELECT topic, splitByChar(':', JSONExtractString(raw, 'id'))[1] AS xid,
        toUInt64OrZero(splitByChar(':', JSONExtractString(raw, 'id'))[2]) AS end_lsn,
        max(JSONExtractUInt(raw, 'event_count')) AS expected
    FROM reporting.cdc_log WHERE JSONExtractString(raw, 'status') = 'END'
    GROUP BY topic, xid, end_lsn
), starts AS (
    SELECT topic, splitByChar(':', JSONExtractString(raw, 'id'))[1] AS xid,
        toUInt64OrZero(splitByChar(':', JSONExtractString(raw, 'id'))[2]) AS start_lsn
    FROM reporting.cdc_log WHERE JSONExtractString(raw, 'status') = 'BEGIN'
), pairs AS (
    SELECT b.topic AS topic, b.xid AS xid, b.end_lsn AS end_lsn, b.expected AS expected,
        max(s.start_lsn) AS start_lsn
    FROM boundaries b INNER JOIN starts s ON b.topic = s.topic AND b.xid = s.xid
    WHERE s.start_lsn <= b.end_lsn
    GROUP BY topic, xid, end_lsn, expected
)
SELECT p.topic AS topic, p.xid AS xid, p.start_lsn AS start_lsn, p.end_lsn AS end_lsn
FROM pairs p INNER JOIN reporting.cdc_events e
    ON p.topic = e.topic AND p.xid = splitByChar(':', e.tx)[1]
WHERE e.op IN ('c', 'u', 'd', 't') AND e.lsn >= p.start_lsn AND e.lsn <= p.end_lsn
GROUP BY topic, xid, start_lsn, end_lsn, p.expected
HAVING uniqExact(e.tx_order) = p.expected;

CREATE VIEW IF NOT EXISTS reporting.cdc_committed AS
SELECT * FROM reporting.cdc_events WHERE op = 'r'
UNION ALL
SELECT e.* FROM reporting.cdc_events e INNER JOIN reporting.cdc_complete_transactions t
    ON e.topic = t.topic AND splitByChar(':', e.tx)[1] = t.xid
WHERE e.op IN ('c', 'u', 'd', 't') AND e.lsn >= t.start_lsn AND e.lsn <= t.end_lsn;

CREATE TABLE IF NOT EXISTS reporting.cdc_state (
    topic LowCardinality(String), table_name LowCardinality(String), row_key String,
    payload String, deleted UInt8, lsn UInt64, last_op LowCardinality(String)
) ENGINE = MergeTree ORDER BY (topic, table_name, row_key);

-- Atomic replacement also removes deleted rows from downstream aggregates.
CREATE MATERIALIZED VIEW IF NOT EXISTS reporting.cdc_state_mv
REFRESH EVERY 10 SECOND TO reporting.cdc_state AS
WITH latest_truncates AS (
    SELECT topic, table_name, max(lsn) AS truncate_lsn
    FROM reporting.cdc_committed WHERE op = 't' GROUP BY topic, table_name
), latest AS (
    SELECT e.topic AS topic, e.table_name AS table_name,
        multiIf(table_name = 'customers', toString(tuple(JSONExtractString(payload, 'customer_id'))),
                table_name = 'prostheses', toString(tuple(JSONExtractString(payload, 'prosthesis_id'))),
                table_name = 'ownership', toString(tuple(JSONExtractString(payload, 'prosthesis_id'), JSONExtractString(payload, 'valid_from'))),
                table_name = 'telemetry', toString(tuple(JSONExtractString(payload, 'event_id'))),
                table_name IN ('export_checkpoint', 'cdc_control'), toString(tuple(JSONExtractBool(payload, 'id'))), '') AS row_key,
        argMax(tuple(payload, toUInt8(op = 'd'), e.lsn, op), tuple(e.lsn, tx_order, offset)) AS state
    FROM reporting.cdc_committed e
    LEFT JOIN latest_truncates t ON e.topic = t.topic AND e.table_name = t.table_name
    WHERE op != 't' AND e.lsn > t.truncate_lsn
    GROUP BY topic, table_name, row_key
)
SELECT topic, table_name, row_key, state.1 AS payload, state.2 AS deleted, state.3 AS lsn, state.4 AS last_op
FROM latest;

CREATE VIEW IF NOT EXISTS reporting.cdc_customers AS
SELECT JSONExtractString(payload, 'customer_id') AS customer_id,
       JSONExtractString(payload, 'keycloak_subject') AS subject
FROM reporting.cdc_state WHERE topic = 'crm.events' AND table_name = 'customers' AND NOT deleted;

CREATE VIEW IF NOT EXISTS reporting.cdc_prostheses AS
SELECT JSONExtractString(payload, 'prosthesis_id') AS prosthesis_id,
       JSONExtractString(payload, 'model') AS model
FROM reporting.cdc_state WHERE topic = 'crm.events' AND table_name = 'prostheses' AND NOT deleted;

CREATE VIEW IF NOT EXISTS reporting.cdc_ownership AS
SELECT JSONExtractString(payload, 'prosthesis_id') AS prosthesis_id,
       JSONExtractString(payload, 'customer_id') AS customer_id,
       parseDateTime64BestEffortOrZero(JSONExtractString(payload, 'valid_from'), 6, 'UTC') AS valid_from,
       parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'valid_to'), 6, 'UTC') AS valid_to
FROM reporting.cdc_state WHERE topic = 'crm.events' AND table_name = 'ownership' AND NOT deleted;

CREATE VIEW IF NOT EXISTS reporting.cdc_telemetry AS
SELECT JSONExtractString(payload, 'event_id') AS event_id,
       JSONExtractString(payload, 'prosthesis_id') AS prosthesis_id,
       parseDateTime64BestEffortOrZero(JSONExtractString(payload, 'occurred_at'), 6, 'UTC') AS occurred_at,
       JSONExtractUInt(payload, 'movements') AS movements,
       JSONExtractFloat(payload, 'response_ms') AS response_ms,
       JSONExtractFloat(payload, 'battery_pct') AS battery_pct,
       toUInt8(JSONExtractBool(payload, 'is_error')) AS is_error
FROM reporting.cdc_state WHERE topic = 'telemetry.events' AND table_name = 'telemetry' AND NOT deleted;

-- Streaming heartbeat updates are issued only after the initial snapshot.
-- last_op excludes snapshot rows regardless of their stored timestamp.
CREATE VIEW IF NOT EXISTS reporting.cdc_invalid_rows AS
SELECT topic, table_name, row_key FROM reporting.cdc_state
WHERE NOT deleted AND topic IN ('crm.events', 'telemetry.events') AND multiIf(
    table_name = 'customers', JSONExtractString(payload, 'customer_id') = '' OR JSONExtractString(payload, 'keycloak_subject') = '',
    table_name = 'prostheses', JSONExtractString(payload, 'prosthesis_id') = '' OR NOT JSONHas(payload, 'model'),
    table_name = 'ownership', JSONExtractString(payload, 'prosthesis_id') = '' OR JSONExtractString(payload, 'customer_id') = ''
        OR parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'valid_from'), 6, 'UTC') IS NULL
        OR (JSONExtractRaw(payload, 'valid_to') != 'null' AND
            parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'valid_to'), 6, 'UTC') IS NULL),
    table_name = 'telemetry', JSONExtractString(payload, 'event_id') = '' OR JSONExtractString(payload, 'prosthesis_id') = ''
        OR parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'occurred_at'), 6, 'UTC') IS NULL
        OR NOT JSONHas(payload, 'movements') OR NOT JSONHas(payload, 'response_ms')
        OR NOT JSONHas(payload, 'battery_pct') OR NOT JSONHas(payload, 'is_error'),
    table_name = 'export_checkpoint', NOT JSONExtractBool(payload, 'id')
        OR parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'closed_through'), 6, 'UTC') IS NULL,
    table_name = 'cdc_control', NOT JSONExtractBool(payload, 'id')
        OR parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'touched_at'), 6, 'UTC') IS NULL, 1);

CREATE VIEW IF NOT EXISTS reporting.cdc_readiness AS
SELECT countIf(table_name = 'export_checkpoint') AS checkpoints,
       minIf(parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'closed_through'), 6, 'UTC'),
             table_name = 'export_checkpoint') AS closed_through,
       countIf(table_name = 'cdc_control' AND last_op IN ('c', 'u') AND
          parseDateTime64BestEffortOrNull(JSONExtractString(payload, 'touched_at'), 6, 'UTC') > now() - INTERVAL 2 MINUTE) AS live_sources
FROM reporting.cdc_state WHERE NOT deleted AND topic IN ('crm.events', 'telemetry.events')
    AND table_name IN ('export_checkpoint', 'cdc_control');

CREATE VIEW IF NOT EXISTS reporting.cdc_invalid_events AS
SELECT e.event_id AS event_id, toDate(e.occurred_at, 'UTC') AS day
FROM reporting.cdc_telemetry e
LEFT JOIN reporting.cdc_ownership o ON e.prosthesis_id = o.prosthesis_id
LEFT JOIN reporting.cdc_customers c ON o.customer_id = c.customer_id
LEFT JOIN reporting.cdc_prostheses p ON e.prosthesis_id = p.prosthesis_id
GROUP BY event_id, day
HAVING countIf(c.subject != '' AND p.prosthesis_id != '' AND o.valid_from <= e.occurred_at
    AND (o.valid_to IS NULL OR e.occurred_at < o.valid_to)) != 1;

CREATE TABLE IF NOT EXISTS reporting.cdc_report_current (
    subject String, day Date, prosthesis_id String, model String,
    samples UInt64, movements UInt64, errors UInt64,
    avg_response_ms Nullable(Float64), max_response_ms Nullable(Float64), min_battery_pct Nullable(Float64),
    row_kind LowCardinality(String), ready UInt8
) ENGINE = MergeTree ORDER BY (subject, day, prosthesis_id);

-- Refreshable MV rebuilds JOINs when either side changes. Summing an INSERT-only
-- JOIN would double count updates and would not react to dimension changes.
CREATE MATERIALIZED VIEW IF NOT EXISTS reporting.cdc_report_current_mv
REFRESH EVERY 10 SECOND DEPENDS ON reporting.cdc_state_mv TO reporting.cdc_report_current AS
WITH owners AS (
    SELECT c.subject AS subject, o.prosthesis_id AS prosthesis_id, p.model AS model,
           o.valid_from AS valid_from, o.valid_to AS valid_to
    FROM reporting.cdc_ownership o
    INNER JOIN reporting.cdc_customers c ON o.customer_id = c.customer_id
    INNER JOIN reporting.cdc_prostheses p ON o.prosthesis_id = p.prosthesis_id
), owner_days AS (
    SELECT subject, prosthesis_id, any(model) AS model,
        addDays(toDate(greatest(valid_from, toDateTime64('2026-01-01', 6, 'UTC')), 'UTC'),
            arrayJoin(range(toUInt32(greatest(0, dateDiff('day',
                toDate(greatest(valid_from, toDateTime64('2026-01-01', 6, 'UTC')), 'UTC'), toDate(now('UTC')))))))) AS day
    FROM owners
    WHERE valid_from < now() AND (valid_to IS NULL OR valid_to > toDateTime64('2026-01-01', 6, 'UTC'))
    GROUP BY subject, prosthesis_id, day
    HAVING countIf(valid_from < toDateTime(day, 'UTC') + INTERVAL 1 DAY AND
        (valid_to IS NULL OR valid_to > toDateTime(day, 'UTC'))) > 0
), totals AS (
    SELECT o.subject AS subject, e.prosthesis_id AS prosthesis_id, toDate(e.occurred_at, 'UTC') AS day,
        count() AS samples, sum(e.movements) AS movements, sum(toUInt64(e.is_error)) AS errors,
        avg(e.response_ms) AS avg_response_ms, max(e.response_ms) AS max_response_ms, min(e.battery_pct) AS min_battery_pct
    FROM reporting.cdc_telemetry e INNER JOIN owners o ON e.prosthesis_id = o.prosthesis_id
    WHERE o.valid_from <= e.occurred_at AND (o.valid_to IS NULL OR e.occurred_at < o.valid_to)
    GROUP BY subject, prosthesis_id, day
)
SELECT o.subject AS subject, o.day AS day, o.prosthesis_id AS prosthesis_id, o.model AS model,
    t.samples AS samples, t.movements AS movements, t.errors AS errors,
    if(t.samples = 0, NULL, t.avg_response_ms) AS avg_response_ms,
    if(t.samples = 0, NULL, t.max_response_ms) AS max_response_ms,
    if(t.samples = 0, NULL, t.min_battery_pct) AS min_battery_pct,
    'report' AS row_kind, toUInt8(1) AS ready
FROM owner_days o LEFT JOIN totals t ON o.subject = t.subject AND o.day = t.day AND o.prosthesis_id = t.prosthesis_id
UNION ALL
SELECT '', d.day, '', '', toUInt64(0), toUInt64(0), toUInt64(0), NULL, NULL, NULL, 'period',
    toUInt8(r.checkpoints = 2 AND r.live_sources = 2 AND
        r.closed_through >= toDateTime(d.day, 'UTC') + INTERVAL 1 DAY AND bad.invalid = 0
        AND (SELECT count() FROM reporting.cdc_invalid_rows) = 0)
FROM (SELECT addDays(toDate('2026-01-01'), arrayJoin(range(toUInt32(greatest(0,
    dateDiff('day', toDate('2026-01-01'), toDate(now('UTC')))))))) AS day) d
CROSS JOIN reporting.cdc_readiness r
LEFT JOIN (SELECT day, count() AS invalid FROM reporting.cdc_invalid_events GROUP BY day) bad ON d.day = bad.day;

-- API reads immutable CDC batches; legacy ETL batches remain available for audit.
CREATE TABLE IF NOT EXISTS reporting.cdc_report_mart AS reporting.report_mart;
CREATE TABLE IF NOT EXISTS reporting.cdc_report_periods AS reporting.report_periods;
ALTER TABLE reporting.cdc_report_periods ADD COLUMN IF NOT EXISTS content_hash String;
