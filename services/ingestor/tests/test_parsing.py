import datetime as dt
import json
from pathlib import Path

import pytest

from ingestor.parsing import (
    RegisterDef,
    reject_bad_reads,
    status_from_label,
    InvalidMessage,
    SeqTracker,
    TagConfig,
    apply_tag_config,
    decode_status,
    decode_telemetry,
    load_validator,
    parse_topic,
)

ROOT = Path(__file__).parents[3]
VALIDATOR = load_validator(ROOT / "schemas/telemetry.v1.json")
NOW = dt.datetime(2026, 9, 26, 10, 15, 31, tzinfo=dt.timezone.utc)


def topic_for(device_id):
    return parse_topic(f"plc/site1/line1/{device_id}/telemetry")


def decode(doc, device_id=None, now=NOW):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return decode_telemetry(topic_for(device_id or doc["device_id"]), raw, now, VALIDATOR)


@pytest.mark.parametrize("path", sorted((ROOT / "schemas/examples").glob("*.json")), ids=lambda p: p.name)
def test_example_payloads_are_valid(path):
    doc = json.loads(path.read_text())
    msg = decode(doc)
    assert msg.device_id == doc["device_id"]
    assert len(msg.readings) == len(doc["tags"])


def test_parse_topic():
    t = parse_topic("plc/pune/line-2/plc_01/telemetry")
    assert (t.site, t.line, t.device_id, t.kind) == ("pune", "line-2", "plc_01", "telemetry")
    assert not t.simulated and parse_topic("sim/pune/line1/demo/telemetry").simulated
    assert parse_topic("other/a/b/c/telemetry") is None
    assert parse_topic("plc/a/b/c/other") is None
    assert parse_topic("plc/a/b/telemetry") is None
    assert parse_topic("plc/a/b/c d/telemetry") is None


def test_value_types_and_quality():
    msg = decode({
        "schema_version": 1, "device_id": "d1", "ts": "2026-09-26T10:15:30Z",
        "tags": {"t": 21.5, "run": True, "count": 7, "recipe": "A"},
        "quality": {"t": "BAD"},
    })
    by = {r.tag: r for r in msg.readings}
    assert (by["t"].value_num, by["t"].quality) == (21.5, 2)
    assert (by["run"].data_type, by["run"].value_num, by["run"].display_value) == ("boolean", 1.0, True)
    assert (by["count"].data_type, by["count"].value_num, by["count"].integral) == ("number", 7.0, True)
    assert not by["t"].integral
    assert (by["recipe"].data_type, by["recipe"].value_text) == ("string", "A")
    assert msg.ts_from_device and msg.ts == dt.datetime(2026, 9, 26, 10, 15, 30, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    "ts, from_device",
    [(None, False), ("not-a-date", False), ("2026-09-26T09:00:00Z", False), ("2026-09-26T10:15:00", True)],
)
def test_timestamp_fallback(ts, from_device):
    doc = {"schema_version": 1, "device_id": "d1", "tags": {"x": 1}}
    if ts:
        doc["ts"] = ts
    msg = decode(doc)
    assert msg.ts_from_device is from_device
    if not from_device:
        assert msg.ts == NOW


@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"{not json", "not JSON"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":NaN}}', "NaN"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":1e999}}', "finite"),
        (b'{"schema_version":2,"device_id":"d1","tags":{"x":1}}', "schema_version"),
        (b'{"schema_version":1,"device_id":"d1","tags":{}}', "tags"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":[1]}}', "tags/x"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":1},"extra":1}', "(root)"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":1},"status":"BROKEN"}', "status"),
        (b'{"schema_version":1,"device_id":"d1","tags":{"x":1},"quality":{"x":"MEH"}}', "quality/x"),
    ],
)
def test_invalid_payloads(payload, reason):
    with pytest.raises(InvalidMessage, match=reason.replace("(", r"\(").replace(")", r"\)")):
        decode(payload, device_id="d1")


def test_device_id_must_match_topic():
    with pytest.raises(InvalidMessage, match="does not match"):
        decode({"schema_version": 1, "device_id": "d1", "tags": {"x": 1}}, device_id="d2")


def test_tag_config_scaling_and_types():
    msg = decode({"schema_version": 1, "device_id": "d1", "tags": {"raw": 100, "flag": 1}})
    raw, flag = msg.readings
    assert apply_tag_config(raw, TagConfig("number", 0.1, -5)).value_num == pytest.approx(5.0)
    assert apply_tag_config(raw, None) is raw
    as_bool = apply_tag_config(flag, TagConfig("boolean"))
    assert as_bool.data_type == "boolean" and as_bool.display_value is True
    assert apply_tag_config(raw, TagConfig("string")).value_text == "100"


@pytest.mark.parametrize(
    "payload, expected",
    [(b"online", True), (b"OFFLINE", False), (b'{"status":"offline"}', False), (b"weird", None), (b"\xff", None)],
)
def test_decode_status(payload, expected):
    assert decode_status(payload) is expected


def test_seq_tracker_duplicates_gaps_and_restart():
    s = SeqTracker(window_s=60)
    assert s.observe("d", 1, now=0) == (False, 0)
    assert s.observe("d", 2, now=1) == (False, 0)
    assert s.observe("d", 2, now=2) == (True, 0)      # QoS 1 redelivery
    assert s.observe("d", 6, now=3) == (False, 3)     # 3, 4, 5 lost
    assert s.observe("d", 0, now=4) == (False, 0)     # PLC restart
    assert s.observe("d", 1, now=100) == (False, 0)   # outside the dedupe window
    assert s.observe("d", None) == (False, 0)
    assert s.observe("other", 2, now=5) == (False, 0)
    s.reset("other")
    assert s.observe("other", 2, now=6) == (False, 0)  # forgotten after reconnect


@pytest.mark.parametrize(
    "payload",
    [
        b"result: [275.500000,138.500000,4.170000, 6.140000,3.050000]",
        b"result:[275.5,138.5,4.17,6.14,3.05]\n",
        b'{"result": [275.5, 138.5, 4.17, 6.14, 3.05]}',
        b'"result": [275.5, 138.5, 4.17, 6.14, 3.05]',
        b"result = [275.5, 138.5, 4.17, 6.14, 3.05];",
    ],
)
def test_simple_array_format(payload):
    msg = decode_telemetry(topic_for("d1"), payload, NOW, VALIDATOR)
    assert msg.format == "simple" and msg.ts == NOW and not msg.ts_from_device
    assert [(r.tag, r.value_num) for r in msg.readings] == [
        ("result_1", 275.5), ("result_2", 138.5), ("result_3", 4.17), ("result_4", 6.14), ("result_5", 3.05),
    ]


def test_simple_object_and_bare_array():
    msg = decode_telemetry(
        topic_for("d1"), b'{"temp": 21, "motor": {"run": true, "amps": [1.5, 2]}, "note": null}', NOW, VALIDATOR
    )
    by = {r.tag: r for r in msg.readings}
    assert set(by) == {"temp", "motor.run", "motor.amps_1", "motor.amps_2"}
    assert by["motor.run"].data_type == "boolean" and by["temp"].integral
    msg = decode_telemetry(topic_for("d1"), b"[1, 2.5]", NOW, VALIDATOR)
    assert [r.tag for r in msg.readings] == ["value_1", "value_2"]


@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"hello", "not JSON"),
        (b"result: [1, 2", "not JSON"),
        (b"42", "expected a JSON object"),
        (b"{}", "expected a JSON object"),
        (b'{"a": null}', "no values"),
        (b'{"bad name!": 1}', "not a valid tag name"),
        (b"result: [1, NaN]", "NaN"),
        (b"\xff\xfe", "UTF-8"),
    ],
)
def test_simple_format_rejections(payload, reason):
    with pytest.raises(InvalidMessage, match=reason):
        decode_telemetry(topic_for("d1"), payload, NOW, VALIDATOR)


# ---- Modbus gateway (PM3032) format, exactly as sent by the real PLC

STATUS_MSG = b'{"PM3032_DATA":[{"server_id":1,"addr":1,"full_addr":"400001","size":3,"data":"[0]","ip":"192.168.18.2","name":"D2"}]}'
VALUES_MSG = (b'{"PM3032_DATA":[{"server_id":1,"addr":2,"full_addr":"400002","size":50,'
              b'"data":"[276.000000,138.500000,4.170000,6.140000,3.050000]","ip":"192.168.18.2","name":"D1"}]}')
PAPER_MACHINE = {
    400001: RegisterDef(400001, "Machine_Status", "int16"),
    400002: RegisterDef(400002, "Machine_Speed", "float32"),
    400004: RegisterDef(400004, "Main_Motor_Current", "float32"),
    400006: RegisterDef(400006, "Dryer_Steam_Pressure", "float32"),
    400008: RegisterDef(400008, "Paper_Moisture", "float32"),
    400010: RegisterDef(400010, "Main_Bearing_Vibration", "float32"),
}


def test_modbus_blocks_with_register_map():
    topic = parse_topic("plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
    status = decode_telemetry(topic, STATUS_MSG, NOW, VALIDATOR, PAPER_MACHINE)
    assert status.format == "modbus"
    assert [(r.tag, r.value_num, r.integral) for r in status.readings] == [("Machine_Status", 0.0, True)]
    values = decode_telemetry(topic, VALUES_MSG, NOW, VALIDATOR, PAPER_MACHINE)
    assert [(r.tag, r.value_num) for r in values.readings] == [
        ("Machine_Speed", 276.0), ("Main_Motor_Current", 138.5), ("Dryer_Steam_Pressure", 4.17),
        ("Paper_Moisture", 6.14), ("Main_Bearing_Vibration", 3.05),
    ]


def test_modbus_blocks_without_map_use_register_addresses():
    topic = parse_topic("plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
    msg = decode_telemetry(topic, VALUES_MSG, NOW, VALIDATOR)
    assert [r.tag for r in msg.readings] == ["reg_400002", "reg_400003", "reg_400004", "reg_400005", "reg_400006"]


def test_modbus_bad_blocks_are_rejected():
    topic = parse_topic("plc/s/l/d/telemetry")
    with pytest.raises(InvalidMessage, match="not a list of numbers"):
        decode_telemetry(topic, b'{"X":[{"full_addr":"400001","data":"[oops]"}]}', NOW, VALIDATOR)
    with pytest.raises(InvalidMessage, match="not a register address"):
        decode_telemetry(topic, b'{"X":[{"full_addr":"abc","data":"[1]"}]}', NOW, VALIDATOR)


@pytest.mark.parametrize(
    "label, status",
    [("Running", "RUN"), ("Stopped", "STOP"), ("E-Stop fault", "FAULT"), ("Idle", "IDLE"),
     ("Maintenance", "MAINT"), ("On", "RUN"), ("Off", "STOP"), ("Mode 3", None), (None, None)],
)
def test_status_from_label(label, status):
    assert status_from_label(label) == status


GARBAGE_STATUS = b'{"PM3032_DATA":[{"server_id":1,"addr":1,"full_addr":"400001","size":8,"data":"[-19157]","ip":"192.168.18.2","name":"D2"}]}'
GARBAGE_VALUES = (b'{"PM3032_DATA":[{"server_id":1,"addr":2,"full_addr":"400002","size":129,"data":"[-44298551296.000000,'
                  b'-28848127533489853006564714317254492160.000000,-103276347031795907145600746913792.000000,-0.000000,0.000000]",'
                  b'"ip":"192.168.18.2","name":"D1"}]}')
CFG = {
    "Machine_Status": TagConfig(value_labels={"0": "Stopped", "1": "Running"}),
    "Machine_Speed": TagConfig(valid_min=0, valid_max=2000),
    "Main_Motor_Current": TagConfig(valid_min=0, valid_max=5000),
    "Dryer_Steam_Pressure": TagConfig(valid_min=0, valid_max=50),
    "Paper_Moisture": TagConfig(valid_min=0, valid_max=100),
    "Main_Bearing_Vibration": TagConfig(valid_min=0, valid_max=100),
}


def test_garbage_gateway_reads_are_rejected_block_wide():
    topic = parse_topic("plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
    status = decode_telemetry(topic, GARBAGE_STATUS, NOW, VALIDATOR, PAPER_MACHINE)
    readings, reasons = reject_bad_reads(status.readings, CFG.get)
    assert readings[0].rejected and readings[0].value_num is None and readings[0].value_text == "-19157"
    assert "not a known code" in reasons[0]

    values = decode_telemetry(topic, GARBAGE_VALUES, NOW, VALIDATOR, PAPER_MACHINE)
    readings, reasons = reject_bad_reads(values.readings, CFG.get)
    # speed/current/pressure are out of range; moisture/vibration (-0, 0) are in range but share the bad read
    assert all(r.rejected and r.quality == 2 and r.value_num is None for r in readings)
    assert len(reasons) == 3


def test_good_gateway_reads_pass_untouched():
    topic = parse_topic("plc/Bijnor/Line1/conveyer-plc-line-01/telemetry")
    msg = decode_telemetry(topic, VALUES_MSG, NOW, VALIDATOR, PAPER_MACHINE)
    readings, reasons = reject_bad_reads(msg.readings, CFG.get)
    assert reasons == [] and not any(r.rejected for r in readings) and readings[0].value_num == 276.0
