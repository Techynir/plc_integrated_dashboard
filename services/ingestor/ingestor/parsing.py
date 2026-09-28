"""Pure message handling: topic parsing, validation, normalisation. No I/O."""

import datetime as dt
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from jsonschema import Draft202012Validator

# plc/... is published by real PLCs; sim/... only by the web simulator (its MQTT account can
# publish nowhere else), so simulated data can never be mistaken for a real device.
TOPIC_RE = re.compile(
    r"^(plc|sim)/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{1,64})/(telemetry|status)$"
)
QUALITY_CODES = {"GOOD": 0, "UNCERTAIN": 1, "BAD": 2}
QUALITY_BAD = 2
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)


class InvalidMessage(ValueError):
    pass


@dataclass(frozen=True)
class Topic:
    root: str  # plc | sim
    site: str
    line: str
    device_id: str
    kind: str  # telemetry | status

    @property
    def simulated(self) -> bool:
        return self.root == "sim"


@dataclass(frozen=True)
class Reading:
    tag: str
    data_type: str  # number | boolean | string
    value_num: float | None
    value_text: str | None
    quality: int
    integral: bool = False  # sent as a JSON integer (counters, codes): shown without decimals
    block: int | None = None  # Modbus gateway: index of the register block (one read) it came from
    rejected: bool = False  # bad read: kept as text for audit, excluded from values/charts/alarms

    @property
    def display_value(self) -> float | bool | str | None:
        if self.data_type == "boolean" and self.value_num is not None:
            return self.value_num != 0
        if self.data_type == "string":
            return self.value_text
        return self.value_num if self.value_num is not None else self.value_text


@dataclass(frozen=True)
class Telemetry:
    device_id: str
    ts: dt.datetime
    ts_from_device: bool
    seq: int | None
    status: str | None
    readings: list[Reading]
    format: str = "canonical"  # canonical | modbus | simple (see decode_modbus / decode_simple)


@dataclass(frozen=True)
class TagConfig:
    data_type: str = "number"
    value_scale: float = 1.0
    value_offset: float = 0.0
    value_labels: dict | None = None  # e.g. {"0": "Stopped", "1": "Running"}
    valid_min: float | None = None
    valid_max: float | None = None


# Device status derived from a labelled status register (first match wins).
_STATUS_WORDS = [
    ("fault", "FAULT"), ("error", "FAULT"), ("alarm", "FAULT"), ("trip", "FAULT"),
    ("maint", "MAINT"), ("idle", "IDLE"), ("standby", "IDLE"), ("ready", "IDLE"),
    ("stop", "STOP"), ("halt", "STOP"), ("off", "STOP"), ("run", "RUN"), ("on", "RUN"),
]


def status_from_label(label: str | None) -> str | None:
    """'Running' -> RUN, 'Stopped' -> STOP, 'E-Stop fault' -> FAULT; None if nothing matches."""
    if not label:
        return None
    words = re.findall(r"[a-z]+", label.lower())
    for keyword, status in _STATUS_WORDS:
        if any(w.startswith(keyword) for w in words):
            return status
    return None


def parse_topic(topic: str) -> Topic | None:
    m = TOPIC_RE.match(topic)
    return Topic(*m.groups()) if m else None


def load_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _reject_constant(name: str):
    raise InvalidMessage(f"invalid JSON: {name} is not allowed")


def resolve_timestamp(raw: str | None, received_at: dt.datetime) -> tuple[dt.datetime, bool]:
    """Return (timestamp, from_device). Falls back to server time when missing, unparsable or skewed."""
    if not raw:
        return received_at, False
    try:
        ts = dt.datetime.fromisoformat(raw)
    except ValueError:
        return received_at, False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if abs(ts - received_at) > MAX_CLOCK_SKEW:
        return received_at, False
    return ts, True


# `result: [1.5, 2]` (not JSON) or `{"result": [1.5, 2]}`: one key holding a list of values.
_KEY_ARRAY_RE = re.compile(r'^\s*\{?\s*"?([A-Za-z0-9_.-]{1,64})"?\s*[:=]\s*(\[.*\])\s*\}?\s*;?\s*$', re.DOTALL)
TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_TAGS = 500


def _load_payload(payload: bytes):
    try:
        text = payload.decode()
    except UnicodeDecodeError as exc:
        raise InvalidMessage("payload is not UTF-8 text") from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        m = _KEY_ARRAY_RE.match(text)
        if m:
            try:
                return {m.group(1): json.loads(m.group(2), parse_constant=_reject_constant)}
            except json.JSONDecodeError:
                pass
        raise InvalidMessage(f"not JSON and not 'name: [values]': {exc}") from exc


def _reading(tag: str, value, quality: int = 0) -> Reading:
    if isinstance(value, bool):
        return Reading(tag, "boolean", 1.0 if value else 0.0, None, quality)
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except OverflowError as exc:
            raise InvalidMessage(f"tag '{tag}' is out of range") from exc
        if not math.isfinite(num):
            raise InvalidMessage(f"tag '{tag}' is not a finite number")
        return Reading(tag, "number", num, None, quality, integral=isinstance(value, int))
    if isinstance(value, str):
        if len(value) > 256:
            raise InvalidMessage(f"tag '{tag}' text is longer than 256 characters")
        return Reading(tag, "string", None, value, quality)
    raise InvalidMessage(f"tag '{tag}' has an unsupported value type")


def decode_simple(topic: Topic, doc, received_at: dt.datetime) -> Telemetry:
    """Formats without the canonical envelope. Lists expand to name_1..name_N,
    nested objects to parent.child:

        result: [275.5, 138.5]          ->  result_1 = 275.5, result_2 = 138.5
        {"temp": 21.5, "run": true}     ->  temp, run
        [1, 2, 3]                       ->  value_1, value_2, value_3
    """
    if isinstance(doc, list):
        doc = {"value": doc}
    if not isinstance(doc, dict) or not doc:
        raise InvalidMessage("expected a JSON object, a JSON array or 'name: [values]'")

    raw_ts = doc.get("ts")
    ts, from_device = resolve_timestamp(raw_ts if isinstance(raw_ts, str) else None, received_at)
    tags: dict[str, object] = {}

    def add(name: str, value, depth: int = 0) -> None:
        if depth > 4:
            raise InvalidMessage(f"'{name[:80]}' is nested too deeply")
        if isinstance(value, dict):
            for key, inner in value.items():
                add(f"{name}.{key}", inner, depth + 1)
        elif isinstance(value, list):
            for i, inner in enumerate(value, start=1):
                add(f"{name}_{i}", inner, depth + 1)
        elif value is not None:
            if not TAG_NAME_RE.match(name):
                raise InvalidMessage(f"'{name[:80]}' is not a valid tag name (letters, digits, _ . - up to 64)")
            tags[name] = value
        if len(tags) > MAX_TAGS:
            raise InvalidMessage(f"more than {MAX_TAGS} values in one message")

    for key, value in doc.items():
        if key in ("ts", "device_id"):
            continue
        add(str(key), value)
    if not tags:
        raise InvalidMessage("message contains no values")

    readings = [_reading(tag, value) for tag, value in tags.items()]
    return Telemetry(topic.device_id, ts, from_device, None, None, readings, format="simple")


# ---------------------------------------------------------------- Modbus register gateways

# Registers occupied by one value of each type (a Modbus holding register is 16 bits).
REGISTER_WIDTH = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "float32": 2, "float64": 4}


@dataclass(frozen=True)
class RegisterDef:
    address: int  # e.g. 400002
    tag: str
    data_type: str  # key of REGISTER_WIDTH


def is_modbus_blocks(doc) -> bool:
    """Gateway format: {"<NAME>": [{"full_addr": "400002", "data": "[276.0, ...]", ...}, ...]}"""
    if not isinstance(doc, dict) or len(doc) != 1:
        return False
    blocks = next(iter(doc.values()))
    return (
        isinstance(blocks, list)
        and len(blocks) > 0
        and all(isinstance(b, dict) and "full_addr" in b and "data" in b for b in blocks)
    )


def _block_values(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise InvalidMessage(f"register data {raw[:60]!r} is not a list of numbers") from exc
    values = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        raise InvalidMessage("register data must contain numbers only")
    return values


def decode_modbus(
    topic: Topic, doc: dict, received_at: dt.datetime, register_map: dict[int, RegisterDef] | None
) -> Telemetry:
    """Each block starts at full_addr; its values fill consecutive registers. A value's width comes
    from the register map (float32 = 2 registers, int16 = 1, ...). Unmapped values become reg_<address>
    and are assumed to be one register wide."""
    register_map = register_map or {}
    readings: list[Reading] = []
    for block_index, block in enumerate(next(iter(doc.values()))):
        try:
            address = int(str(block["full_addr"]).strip())
        except ValueError as exc:
            raise InvalidMessage(f"full_addr {block['full_addr']!r} is not a register address") from exc
        for value in _block_values(block["data"]):
            reg = register_map.get(address)
            if reg is None:
                readings.append(replace(_reading(f"reg_{address}", value), block=block_index))
                address += 1
                continue
            reading = replace(_reading(reg.tag, value), block=block_index)
            if reg.data_type == "bool":
                reading = replace(reading, data_type="boolean", value_num=1.0 if value else 0.0)
            elif reg.data_type in ("int16", "uint16", "int32", "uint32"):
                reading = replace(reading, integral=True)
            readings.append(reading)
            address += REGISTER_WIDTH.get(reg.data_type, 1)
        if len(readings) > MAX_TAGS:
            raise InvalidMessage(f"more than {MAX_TAGS} values in one message")
    if not readings:
        raise InvalidMessage("message contains no register values")
    return Telemetry(topic.device_id, received_at, False, None, None, readings, format="modbus")


def decode_telemetry(
    topic: Topic,
    payload: bytes,
    received_at: dt.datetime,
    validator: Draft202012Validator,
    register_map: dict[int, RegisterDef] | None = None,
) -> Telemetry:
    """Canonical v1 messages (with schema_version) are validated strictly against the JSON Schema;
    Modbus gateway blocks are decoded with the device's register map; anything else goes through
    the lenient ``decode_simple``."""
    doc = _load_payload(payload)
    if is_modbus_blocks(doc):
        return decode_modbus(topic, doc, received_at, register_map)
    if not (isinstance(doc, dict) and "schema_version" in doc):
        return decode_simple(topic, doc, received_at)

    error = next(iter(sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise InvalidMessage(f"schema violation at {where}: {error.message[:200]}")

    if doc["device_id"] != topic.device_id:
        raise InvalidMessage(f"device_id '{doc['device_id']}' does not match topic '{topic.device_id}'")

    ts, from_device = resolve_timestamp(doc.get("ts"), received_at)
    quality = doc.get("quality", {})
    readings = [_reading(tag, value, QUALITY_CODES[quality.get(tag, "GOOD")]) for tag, value in doc["tags"].items()]
    return Telemetry(topic.device_id, ts, from_device, doc.get("seq"), doc.get("status"), readings)


def apply_tag_config(reading: Reading, cfg: TagConfig | None) -> Reading:
    """Apply admin-configured type and linear scaling (value * scale + offset)."""
    if cfg is None:
        return reading
    if reading.data_type == "number" and cfg.data_type == "number":
        if cfg.value_scale != 1.0 or cfg.value_offset != 0.0:
            return replace(reading, value_num=reading.value_num * cfg.value_scale + cfg.value_offset)
        return reading
    if reading.value_num is not None and cfg.data_type == "boolean":
        return replace(reading, data_type="boolean")
    if cfg.data_type == "string" and reading.value_text is None:
        text = "" if reading.value_num is None else f"{reading.value_num:g}"
        return replace(reading, data_type="string", value_text=text)
    return reading


def reject_bad_reads(readings: list[Reading], cfg_for) -> tuple[list[Reading], list[str]]:
    """Mark readings outside their valid range, or status codes without a label, as rejected.
    One bad value rejects its whole Modbus block, because a block is a single read and a partly
    garbage read cannot be trusted. Returns (readings, reasons)."""
    reasons: list[str] = []
    bad_blocks: set[int] = set()
    flags = []
    for r in readings:
        cfg = cfg_for(r.tag)
        v = r.value_num
        bad = False
        if cfg is not None and v is not None and r.data_type != "string":
            if cfg.valid_min is not None and v < cfg.valid_min:
                bad = True
                reasons.append(f"{r.tag}={v:g} below valid minimum {cfg.valid_min:g}")
            elif cfg.valid_max is not None and v > cfg.valid_max:
                bad = True
                reasons.append(f"{r.tag}={v:g} above valid maximum {cfg.valid_max:g}")
            elif cfg.value_labels and f"{v:g}" not in cfg.value_labels:
                bad = True
                reasons.append(f"{r.tag}={v:g} is not a known code ({', '.join(cfg.value_labels)})")
        if bad and r.block is not None:
            bad_blocks.add(r.block)
        flags.append(bad)
    out = []
    for r, bad in zip(readings, flags):
        if bad or (r.block is not None and r.block in bad_blocks):
            text = r.value_text if r.value_num is None else f"{r.value_num:g}"
            r = replace(r, value_num=None, value_text=text, quality=QUALITY_BAD, rejected=True)
        out.append(r)
    return out, reasons


def decode_status(payload: bytes) -> bool | None:
    """Status topic payload: 'online' / 'offline', or {"status": "..."}. Returns None if unrecognised."""
    try:
        text = payload.decode().strip()
        if text.startswith("{"):
            text = str(json.loads(text).get("status", ""))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return {"online": True, "offline": False}.get(text.lower())


class SeqTracker:
    """Per-device sequence bookkeeping.

    * A seq seen again within ``window_s`` is a duplicate (QoS 1 redelivery) and is dropped.
    * Forward jumps are counted as gaps (lost messages). Backward jumps mean the PLC restarted.
    * ``reset`` forgets a device, e.g. when it (re)connects and its counter may start over.
    """

    MAX_PLAUSIBLE_GAP = 1_000_000

    def __init__(self, window_s: float = 60.0, max_entries: int = 1024) -> None:
        self.window_s = window_s
        self.max_entries = max_entries
        self._last: dict[str, int] = {}
        self._recent: dict[str, OrderedDict[int, float]] = {}

    def reset(self, device_id: str) -> None:
        self._last.pop(device_id, None)
        self._recent.pop(device_id, None)

    def observe(self, device_id: str, seq: int | None, now: float | None = None) -> tuple[bool, int]:
        """Returns (is_duplicate, gap)."""
        if seq is None:
            return False, 0
        now = time.monotonic() if now is None else now
        recent = self._recent.setdefault(device_id, OrderedDict())
        while recent and (next(iter(recent.values())) < now - self.window_s or len(recent) > self.max_entries):
            recent.popitem(last=False)
        if seq in recent:
            return True, 0

        gap = 0
        last = self._last.get(device_id)
        if last is not None and seq > last + 1:
            gap = seq - last - 1
            if gap > self.MAX_PLAUSIBLE_GAP:
                gap = 0
        self._last[device_id] = seq
        recent[seq] = now
        return False, gap
