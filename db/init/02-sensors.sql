-- Sensor identity is positional: position = row order in the sensor server's
-- push form (0-based). Reordering rows there means updating this table.
CREATE TABLE sensors (
    position       INT PRIMARY KEY,
    code           TEXT NOT NULL,
    point          TEXT NOT NULL DEFAULT 'temp',  -- 'temp' (Probe 1) or 'on'
    name           TEXT NOT NULL,
    high_threshold DOUBLE PRECISION,
    active         BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (code, point)
);

INSERT INTO sensors (position, code, name) VALUES
    (0,  'RS1-001', 'Room 1'),
    (1,  'RS1-002', 'Room 2'),
    (2,  'RS1-003', 'Room 3 System 1'),
    (3,  'RS1-004', 'Room 4'),
    (4,  'RS1-005', 'Room 5'),
    (5,  'RS1-006', 'Room 5 (Small System)'),
    (6,  'RS1-007', 'Room 5 System 3'),
    (7,  'RS1-008', 'Room 5 System 1'),
    (8,  'RS1-009', 'Room 5 System 2'),
    (9,  'RS1-010', 'Room 5 System 4'),
    (10, 'RS1-011', 'Room 8'),
    (11, 'RS1-012', 'Room 3 System 2'),
    (12, 'RS1-013', 'Room 1 System 2'),
    (13, 'RS1-014', 'Room 3 System 3'),
    (14, 'RS1-015', 'Room 6 Blast Freezer'),
    (15, 'RS1-020', 'Room 7 System 1'),
    (16, 'RS1-022', 'Room 7 System 2');

-- Positions 17-33: the 'On' status of each room, same order as above.
INSERT INTO sensors (position, code, point, name)
SELECT position + 17, code, 'on', name FROM sensors WHERE point = 'temp';

-- One row per UDP datagram, kept verbatim.
CREATE TABLE packets (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    client      TEXT,
    raw         TEXT NOT NULL,
    identifier  TEXT,
    value_count INT,
    sensor_time TIMESTAMP,
    status      TEXT,
    error       TEXT
);
CREATE INDEX packets_received_at_idx ON packets (received_at DESC);

-- A fault marker from the sensor server (-, *, #, /) is stored as value NULL
-- with the marker in fault.
ALTER TABLE readings ALTER COLUMN value DROP NOT NULL;
ALTER TABLE readings ADD COLUMN fault TEXT;
ALTER TABLE readings ADD COLUMN point TEXT NOT NULL DEFAULT 'temp';
ALTER TABLE readings ADD COLUMN sensor_time TIMESTAMP;
ALTER TABLE readings ADD COLUMN packet_id BIGINT;

-- One row per room/device. device_name is what the sensor server calls it;
-- label is the custom name the user can change (NULL = use device_name).
CREATE TABLE rooms (
    code        TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    label       TEXT
);
INSERT INTO rooms (code, device_name)
SELECT code, name FROM sensors WHERE point = 'temp';

CREATE VIEW room_names AS
SELECT code, device_name, label, COALESCE(label, device_name) AS display_name
FROM rooms;
