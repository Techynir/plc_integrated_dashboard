import datetime as dt
import json
from pathlib import Path

import pytest

from ingestor.parsing import (
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
