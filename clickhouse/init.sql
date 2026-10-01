CREATE DATABASE IF NOT EXISTS reporting;

-- Each batch is immutable. Unpublished or failed batches are invisible to API.
CREATE TABLE IF NOT EXISTS reporting.report_mart (
    subject String,
    day Date,
    prosthesis_id String,
    model String,
    samples UInt64,
    movements UInt64,
    errors UInt64,
    avg_response_ms Nullable(Float64),
    max_response_ms Nullable(Float64),
    min_battery_pct Nullable(Float64),
    batch_id UUID
) ENGINE = MergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (subject, day, prosthesis_id, batch_id);

-- Publication occurs only after synchronous data insertion and count verification.
CREATE TABLE IF NOT EXISTS reporting.report_periods (
    day Date,
    batch_id UUID,
    row_count UInt64,
    version UInt64,
    published_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(version)
ORDER BY day;
