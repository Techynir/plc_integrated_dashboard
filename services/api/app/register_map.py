"""Modbus register maps: parsing the plain-text mapping a PLC programmer hands over.

    Machine_Status - 400001 - 16Bit Integer 0=Stopped, 1=Running
    Machine_Speed - 400002 - 32 Bit Real - 275.5
"""

import re
from dataclasses import asdict, dataclass, field

WIDTH = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "float32": 2, "float64": 4}
TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STATUS_WORDS = ("run", "stop", "idle", "fault", "alarm", "trip", "maint", "standby")


@dataclass
class Register:
    address: int
    tag: str
    data_type: str
    value_labels: dict[str, str] | None = None
    is_status: bool = False
    unit: str = ""
    display_name: str = ""


@dataclass
class ParseResult:
    registers: list[Register] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"registers": [asdict(r) for r in self.registers], "warnings": self.warnings, "errors": self.errors}


def parse_type(text: str) -> str | None:
    t = text.lower().replace(" ", "").replace("-", "")
    bits = re.search(r"(16|32|64)bit|(?:int|real|float)(16|32|64)", t)
    size = next((g for g in (bits.groups() if bits else ()) if g), None)
    if "bool" in t or "coil" in t:
        return "bool"
    if "real" in t or "float" in t or "double" in t:
        return "float64" if size == "64" or "double" in t else "float32"
    if "int" in t or "word" in t:
        unsigned = "uint" in t or "unsigned" in t
        return ("uint" if unsigned else "int") + ("32" if size == "32" else "16")
    return None


def parse_address(text: str) -> tuple[int | None, str | None]:
    """Holding registers are 400001-465536. A 7-digit '4000010' (extra zero) is read as 400010."""
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None, None
    value = int(digits)
    if len(digits) == 7 and digits.startswith("40") and int(digits[2:]) <= 65536:
        fixed = 400000 + int(digits[2:])
        return fixed, f"address {digits} read as {fixed} (holding registers have 6 digits)"
    return value, None


def parse_labels(text: str) -> dict[str, str] | None:
    labels = {m.group(1): m.group(2).strip() for m in re.finditer(r"(-?\d+)\s*=\s*([^,;=]+?)(?=\s*(?:[,;]|-?\d+\s*=|$))", text)}
    return labels or None


def parse_mapping(text: str) -> ParseResult:
    result = ParseResult()
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"\s+-\s+|\t|\s*\|\s*", line) if p.strip()]
        if len(parts) < 3:
            result.errors.append(f"line {n}: expected 'Name - Address - Type', got {line!r}")
            continue
        name, address_text, rest = parts[0], parts[1], " ".join(parts[2:])
        tag = re.sub(r"\s+", "_", name)
        if not TAG_RE.match(tag):
            result.errors.append(f"line {n}: {name!r} is not a valid tag name (letters, digits, _ . -)")
            continue
        address, note = parse_address(address_text)
        if address is None:
            result.errors.append(f"line {n}: {address_text!r} is not a register address")
            continue
        if note:
            result.warnings.append(f"line {n}: {note}")
        data_type = parse_type(rest)
        if data_type is None:
            result.errors.append(f"line {n}: cannot tell the data type from {rest!r} (e.g. '16Bit Integer', '32 Bit Real')")
            continue
        labels = parse_labels(rest)
        result.registers.append(Register(address, tag, data_type, labels))

    check_layout(result)
    if not any(r.is_status for r in result.registers):
        for r in result.registers:
            if r.value_labels and any(w in v.lower() for v in r.value_labels.values() for w in STATUS_WORDS):
                r.is_status = True  # e.g. 0=Stopped, 1=Running drives the device status
                break
    return result


def check_layout(result: ParseResult) -> None:
    regs = sorted(result.registers, key=lambda r: r.address)
    seen_tags: set[str] = set()
    for prev, cur in zip(regs, regs[1:]):
        end = prev.address + WIDTH[prev.data_type] - 1
        if cur.address <= end:
            result.errors.append(
                f"{cur.tag} at {cur.address} overlaps {prev.tag} ({prev.data_type} uses {prev.address}-{end})"
            )
    for r in regs:
        if r.tag in seen_tags:
            result.errors.append(f"tag {r.tag} is mapped twice")
        seen_tags.add(r.tag)
    result.registers = regs
