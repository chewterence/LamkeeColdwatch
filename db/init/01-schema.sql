CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every request the ingest service receives, stored verbatim.
-- Used to discover what the sensor server actually sends.
CREATE TABLE raw_requests (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    client      TEXT,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    query       TEXT,
    headers     JSONB,
    body        TEXT
);

CREATE INDEX raw_requests_received_at_idx ON raw_requests (received_at DESC);

-- Parsed readings. Adjust once the real payload format is known.
CREATE TABLE readings (
    time      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sensor_id TEXT NOT NULL,
    value     DOUBLE PRECISION NOT NULL,
    unit      TEXT NOT NULL DEFAULT 'C'
);

SELECT create_hypertable('readings', 'time');

CREATE INDEX readings_sensor_time_idx ON readings (sensor_id, time DESC);
