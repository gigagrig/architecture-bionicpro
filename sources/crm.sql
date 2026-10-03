CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    keycloak_subject TEXT UNIQUE NOT NULL
);
CREATE TABLE prostheses (
    prosthesis_id TEXT PRIMARY KEY,
    model TEXT NOT NULL
);
CREATE TABLE ownership (
    prosthesis_id TEXT NOT NULL REFERENCES prostheses,
    customer_id TEXT NOT NULL REFERENCES customers,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (prosthesis_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&)
);
-- The source declares intervals closed for export, including empty days.
CREATE TABLE export_checkpoint (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    closed_through TIMESTAMPTZ NOT NULL
);
