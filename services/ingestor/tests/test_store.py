"""Device loading takes the gateway poll interval from asset_config, with a fallback."""

import asyncio

from ingestor.store import Store


class FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows


def row(device_id, poll_ms, expected=2.0):
    return {"device_id": device_id, "expected_interval_s": expected, "enabled": True, "online": False,
            "last_seen": None, "status": None, "simulated": False, "poll_ms": poll_ms}


def test_poll_interval_from_asset_config_or_expected_interval():
    devices = asyncio.run(Store(FakePool([row("pm", "500"), row("other", None), row("bad", '"x"')])).load_devices())
    assert devices["pm"].poll_s == 0.5
    assert devices["other"].poll_s == 2.0  # no asset_config entry: the device's expected interval
    assert devices["bad"].poll_s == 2.0
