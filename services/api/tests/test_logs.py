import datetime as dt

from app.routers.logs import build_filter, classify, regex_literal, to_entry

T0 = dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(hours=1)


def test_filter_for_component_text_and_level():
    f = build_filter("proj", "ingestor", 'plc "01"', "error", T0, T1)
    assert 'logName="projects/proj/logs/gcplogs-docker-driver"' in f
    assert 'jsonPayload.container.name=~"^/plc-integrated-dashboard-(ingestor)-[0-9]+$"' in f
    assert 'jsonPayload.message:"plc \\"01\\""' in f  # quotes escaped
    assert "ERROR|CRITICAL" in f and 'timestamp>="2026-09-27T10:00:00+00:00"' in f


def test_key_value_search_has_no_backslash_regex_escapes():
    # Cloud Logging mishandles "\." / "\b" inside regexes; only \" (quote escape) may appear.
    f = build_filter("proj", None, "logger=api.broker", "warning", T0, T1)
    assert '(\\"logger\\": ?\\"?api[.]broker|logger=api[.]broker)' in f
    assert "\\." not in f and "\\b" not in f
    assert "(api|ingestor|mosquitto|web|simulator-ui|db|init|certs)" in f


def test_regex_literal():
    assert regex_literal("a.b(c)*") == "a[.]b[(]c[)][*]"
    assert regex_literal("x^y\\z]") == "x.y.z."


def test_classify_and_entry_parsing():
    assert classify('{"severity": "ERROR", "message": "boom"}') == "error"
    assert classify("2026-09-27T10:00:00: Client x disconnected: Protocol error.") == "error"
    assert classify('{"level":"warn","msg":"slow"}') == "warning"
    assert classify("New client connected") == "info"
    e = to_entry({"timestamp": "t", "jsonPayload": {
        "container": {"name": "/plc-integrated-dashboard-simulator-ui-1"},
        "message": '{"severity": "INFO", "logger": "simulator", "message": "hi"}'}})
    assert e["component"] == "simulator-ui" and e["fields"]["logger"] == "simulator" and e["level"] == "info"
