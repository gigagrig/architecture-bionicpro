CREATE TABLE telemetry (
    event_id TEXT PRIMARY KEY,
    prosthesis_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    movements INTEGER NOT NULL CHECK (movements >= 0),
    response_ms DOUBLE PRECISION NOT NULL CHECK (response_ms >= 0 AND response_ms < 'Infinity'),
    battery_pct DOUBLE PRECISION NOT NULL CHECK (battery_pct BETWEEN 0 AND 100),
    is_error BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX telemetry_period ON telemetry (occurred_at, prosthesis_id);
CREATE TABLE export_checkpoint (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    closed_through TIMESTAMPTZ NOT NULL
);
