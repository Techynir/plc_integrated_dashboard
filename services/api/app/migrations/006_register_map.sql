-- Modbus register maps: which register address holds which tag, for devices that publish through
-- a register gateway ({"...": [{"full_addr": "400002", "data": "[...]"}]}). Value labels turn codes
-- into text (0 = Stopped, 1 = Running); the register marked is_status also drives the device status.

ALTER TABLE tags ADD COLUMN IF NOT EXISTS value_labels jsonb;

CREATE TABLE IF NOT EXISTS register_map (
    device_id text NOT NULL REFERENCES devices ON DELETE CASCADE,
    address   integer NOT NULL CHECK (address > 0),
    tag       text NOT NULL,
    data_type text NOT NULL CHECK (data_type IN ('int16', 'uint16', 'int32', 'uint32', 'float32', 'float64', 'bool')),
    is_status boolean NOT NULL DEFAULT false,
    PRIMARY KEY (device_id, address)
);
