"""PLC simulator: publishes realistic telemetry for N virtual PLCs over MQTT.

Credentials are provisioned through the dashboard API (the same flow an admin
uses for a real PLC), or a single device can be simulated with explicit
credentials:

    # N devices, auto-provisioned via the API
    python simulate.py --devices 6 --api http://localhost:8080 --api-user admin@example.com --api-password ...

    # one device with credentials from the admin UI, over TLS
    python simulate.py --device-id plc-filler-01 --password XXXX --host demo.example.com --port 8883 --ca ca.crt
"""

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import random
import ssl
import sys

import aiomqtt
import httpx

log = logging.getLogger("simulator")


# ---------------------------------------------------------------- machine models


class Machine:
    kind = "generic"

    def __init__(self, device_id: str, rng: random.Random) -> None:
        self.device_id = device_id
        self.rng = rng
        self.t0 = rng.uniform(0, 1000)
        self.status = "RUN"
        self.fault_until = 0.0

    def wave(self, t: float, period: float, amp: float) -> float:
        return amp * math.sin(2 * math.pi * (t + self.t0) / period)

    def noise(self, sigma: float) -> float:
        return self.rng.gauss(0, sigma)

    def step_status(self, t: float, fault_rate: float) -> None:
        if self.status == "FAULT" and t >= self.fault_until:
            self.status = "RUN"
        elif self.status == "RUN" and self.rng.random() < fault_rate:
            self.status = "FAULT"
            self.fault_until = t + self.rng.uniform(20, 60)

    def tags(self, t: float, dt_s: float) -> tuple[dict, dict]:
        raise NotImplementedError


class FillingLine(Machine):
    kind = "filler"

    def __init__(self, *a) -> None:
        super().__init__(*a)
        self.bottles = self.rng.randint(10_000, 20_000)
        self.rejects = self.rng.randint(0, 100)

    def tags(self, t, dt_s):
        running = self.status == "RUN"
        speed = (118 + self.wave(t, 600, 6) + self.noise(1.5)) if running else 0.0
        self.bottles += int(speed * dt_s / 60 + self.rng.random())
        if running and self.rng.random() < 0.02:
            self.rejects += 1
        return {
            "speed_bpm": round(speed, 1),
            "fill_volume_ml": round(500 + self.wave(t, 300, 1.2) + self.noise(0.4), 2),
            "product_temp_c": round(6.5 + self.wave(t, 1800, 1.5) + self.noise(0.1), 2),
            "bottle_count": self.bottles,
            "reject_count": self.rejects,
            "door_closed": self.rng.random() > 0.01,
            "recipe": "COLA-500",
        }, {}


class Boiler(Machine):
    kind = "boiler"

    def tags(self, t, dt_s):
        burner = self.status == "RUN"
        pressure = 9.8 + self.wave(t, 900, 0.6) + self.noise(0.05) if burner else 4 + self.noise(0.1)
        flue = 210 + self.wave(t, 1200, 25) + self.noise(2)
        quality = {"flue_gas_temp_c": "UNCERTAIN"} if self.rng.random() < 0.03 else {}
        return {
            "steam_pressure_bar": round(pressure, 3),
            "water_level_pct": round(60 + self.wave(t, 240, 8) + self.noise(0.8), 1),
            "flue_gas_temp_c": round(flue, 1),
            "fuel_flow_lph": round(34 + self.wave(t, 900, 4) + self.noise(0.5), 2) if burner else 0.0,
            "burner_on": burner,
            "feed_pump_running": self.wave(t, 240, 1) > -0.2,
        }, quality


class Conveyor(Machine):
    kind = "conveyor"

    def tags(self, t, dt_s):
        running = self.status == "RUN"
        temp = 55 + self.wave(t, 1500, 22) + self.noise(0.6)  # periodically crosses 70 C
        quality = {"belt_speed_mps": "BAD"} if not running else {}
        return {
            "belt_speed_mps": round(1.2 + self.noise(0.02), 3) if running else 0.0,
            "motor_current_a": round(12 + self.wave(t, 400, 1.5) + self.noise(0.3), 2) if running else 0.2,
            "motor_temp_c": round(temp, 1),
            "motor_running": running,
            "estop_active": self.status == "FAULT",
            "fault_code": 214 if self.status == "FAULT" else 0,
        }, quality


MACHINES = [FillingLine, Boiler, Conveyor]


# ---------------------------------------------------------------- provisioning


async def provision(api: str, user: str, password: str, specs: list[dict]) -> dict[str, str]:
    """Create each device via the API (or rotate its password) and return {device_id: mqtt_password}."""
    creds = {}
    async with httpx.AsyncClient(base_url=api.rstrip("/"), timeout=15) as http:
        r = await http.post("/api/v1/auth/login", json={"email": user, "password": password})
        r.raise_for_status()
        # In production the session cookie is marked Secure (HTTPS only), but the simulator talks
        # to the API over the internal plain-HTTP network, so send the session explicitly.
        http.headers["Cookie"] = f"plc_session={r.cookies['plc_session']}"
        for spec in specs:
            r = await http.post("/api/v1/devices", json=spec)
            if r.status_code == 409:
                r = await http.post(f"/api/v1/devices/{spec['device_id']}/credentials")
            r.raise_for_status()
            creds[spec["device_id"]] = r.json()["credentials"]["password"]
            log.info("provisioned %s", spec["device_id"])

        # Seed demo alarm rules once, so alarms fire during a demo.
        r = await http.get("/api/v1/alarm-rules")
        if r.status_code == 200 and not r.json():
            for rule in DEMO_RULES:
                (await http.post("/api/v1/alarm-rules", json=rule)).raise_for_status()
            log.info("created %d demo alarm rules", len(DEMO_RULES))
    return creds


DEMO_RULES = [
    {"name": "Motor overtemperature", "tag": "motor_temp_c", "rule_type": "high", "threshold": 72,
     "deadband": 3, "severity": "warning"},
    {"name": "Steam over-pressure", "tag": "steam_pressure_bar", "rule_type": "high", "threshold": 10.35,
     "deadband": 0.2, "severity": "critical"},
    {"name": "Emergency stop", "tag": "estop_active", "rule_type": "equals", "threshold": 1, "severity": "critical",
     "message": "{device}: emergency stop pressed"},
    {"name": "Device fault", "rule_type": "fault", "severity": "critical"},
    {"name": "Device offline", "rule_type": "offline", "threshold": 60, "severity": "warning"},
]


# ---------------------------------------------------------------- device loop


async def run_device(machine: Machine, password: str, args, site: str, line: str) -> None:
    base = f"plc/{site}/{line}/{machine.device_id}"
    tls = None
    if args.ca or args.port == 8883:
        tls = ssl.create_default_context(cafile=args.ca) if args.ca else ssl.create_default_context()
        if args.insecure:
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
    will = aiomqtt.Will(f"{base}/status", "offline", qos=1, retain=True)
    seq = 0
    await asyncio.sleep(machine.rng.uniform(0, args.interval))  # spread devices over the interval

    while True:
        try:
            async with aiomqtt.Client(
                hostname=args.host, port=args.port, username=machine.device_id, password=password,
                identifier=machine.device_id, tls_context=tls, will=will, keepalive=30,
            ) as client:
                await client.publish(f"{base}/status", "online", qos=1, retain=True)
                log.info("%s connected", machine.device_id)
                loop = asyncio.get_running_loop()
                start = last = loop.time()
                while True:
                    now = loop.time()
                    t = now - start
                    machine.step_status(t, args.fault_rate)
                    tags, quality = machine.tags(t, now - last)
                    last = now
                    seq += 1
                    doc = {
                        "schema_version": 1,
                        "device_id": machine.device_id,
                        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "seq": seq,
                        "status": machine.status,
                        "tags": tags,
                    }
                    if quality:
                        doc["quality"] = quality
                    payload = json.dumps(doc)
                    if args.format == "result":
                        # Array-only format some PLC programs emit: result: [v1,v2,...] (6 decimals, no names)
                        numbers = [v for v in tags.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
                        payload = "result: [" + ",".join(f"{v:f}" for v in numbers[:5]) + "]"
                    if machine.rng.random() < args.invalid_rate:
                        payload = json.dumps({**doc, "tags": {"bad": [1, 2]}})  # schema violation demo
                    await client.publish(f"{base}/telemetry", payload, qos=1)
                    await asyncio.sleep(args.interval)
        except aiomqtt.MqttError as exc:
            log.warning("%s connection error: %s (reconnecting in 5s)", machine.device_id, exc)
            await asyncio.sleep(5)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--ca", help="CA certificate for TLS (download from the admin UI)")
    p.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    p.add_argument("--devices", type=int, default=6, help="number of simulated PLCs (API provisioning)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between messages per device")
    p.add_argument("--site", default="pune")
    p.add_argument("--fault-rate", type=float, default=0.002, help="probability per message of a FAULT episode")
    p.add_argument("--invalid-rate", type=float, default=0.0, help="probability per message of a malformed payload")
    p.add_argument("--api", help="dashboard base URL for auto-provisioning, e.g. http://localhost:8080")
    p.add_argument("--api-user")
    p.add_argument("--api-password")
    p.add_argument("--device-id", help="simulate one device with explicit credentials")
    p.add_argument("--password", help="MQTT password for --device-id")
    p.add_argument("--line", default="line1", help="line for --device-id")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--format", choices=["canonical", "result"], default="canonical",
                   help="payload format: canonical JSON (default) or 'result: [v1,v2,...]'")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rng = random.Random(args.seed)

    if args.device_id:
        if not args.password:
            p.error("--password is required with --device-id")
        kind = next((m for m in MACHINES if m.kind in args.device_id), FillingLine)
        await run_device(kind(args.device_id, rng), args.password, args, args.site, args.line)
        return

    if not (args.api and args.api_user and args.api_password):
        p.error("use --api/--api-user/--api-password for auto-provisioning, or --device-id/--password")

    plan = []
    for i in range(args.devices):
        cls = MACHINES[i % len(MACHINES)]
        n = i // len(MACHINES) + 1
        line = f"line{(i % 2) + 1}"
        device_id = f"plc-{cls.kind}-{n:02d}"
        plan.append((cls, device_id, line))
    specs = [
        {"device_id": d, "name": f"{cls.kind.title()} {d[-2:]}", "site": args.site, "line": line,
         "expected_interval_s": args.interval}
        for cls, d, line in plan
    ]

    for attempt in range(30):
        try:
            creds = await provision(args.api, args.api_user, args.api_password, specs)
            break
        except httpx.HTTPError as exc:
            log.warning("API not ready (%s), retrying", exc)
            await asyncio.sleep(3)
    else:
        sys.exit("could not provision devices through the API")

    await asyncio.gather(*(
        run_device(cls(d, random.Random(rng.random())), creds[d], args, args.site, line) for cls, d, line in plan
    ))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
