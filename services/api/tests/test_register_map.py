from app.register_map import parse_address, parse_labels, parse_mapping, parse_type

USER_MAPPING = """Machine_Status - 400001 - 16Bit Integer 0=Stopped, 1=Running
Machine_Speed - 400002 - 32 Bit Real - 275.5
Main_Motor_Current - 400004 - 32 Bit Real - 138.5
Dryer_Steam_Pressure - 400006 - 32 Bit Real - 4.17
Paper_Moisture - 400008 - 32 Bit Real - 6.14
Main_Bearing_Vibration - 4000010 - 32 Bit Real - 3.05
"""


def test_parses_the_mapping_as_given():
    r = parse_mapping(USER_MAPPING)
    assert r.errors == []
    assert [(x.address, x.tag, x.data_type) for x in r.registers] == [
        (400001, "Machine_Status", "int16"),
        (400002, "Machine_Speed", "float32"),
        (400004, "Main_Motor_Current", "float32"),
        (400006, "Dryer_Steam_Pressure", "float32"),
        (400008, "Paper_Moisture", "float32"),
        (400010, "Main_Bearing_Vibration", "float32"),
    ]
    status = r.registers[0]
    assert status.value_labels == {"0": "Stopped", "1": "Running"} and status.is_status
    assert r.warnings == ["line 6: address 4000010 read as 400010 (holding registers have 6 digits)"]
    assert r.registers[1].value_labels is None  # "275.5" is an example value, not a label


def test_types_addresses_labels():
    assert parse_type("16Bit Integer") == "int16"
    assert parse_type("32 Bit Real") == "float32"
    assert parse_type("32-bit unsigned int") == "uint32"
    assert parse_type("Float64") == "float64"
    assert parse_type("Bool") == "bool"
    assert parse_type("mystery") is None
    assert parse_address("400012") == (400012, None)
    assert parse_labels("0=Off; 1=On;2=Fault") == {"0": "Off", "1": "On", "2": "Fault"}


def test_overlap_and_errors_are_reported():
    r = parse_mapping("A - 400002 - 32 Bit Real\nB - 400003 - 16Bit Integer\nC - abc - Real\nD 400009")
    assert any("overlaps" in e for e in r.errors)
    assert any("not a register address" in e for e in r.errors)
    assert any("expected 'Name - Address - Type'" in e for e in r.errors)
