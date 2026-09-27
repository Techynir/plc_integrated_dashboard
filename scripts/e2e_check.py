"""End-to-end smoke test against a running stack.

    docker compose exec -T api python - < scripts/e2e_check.py

Uses the API over HTTP and publishes as a freshly provisioned device over MQTT.
Exits non-zero on the first failed check.
"""

import asyncio
import datetime as dt
import json
import os
import sys

import aiomqtt
import httpx

API = os.environ.get("E2E_API", "http://localhost:8000")
SIM = os.environ.get("E2E_SIM", "http://simulator-ui:8000")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
ADMIN = (os.environ.get("ADMIN_EMAIL", "admin@example.com"), os.environ.get("ADMIN_PASSWORD", "admin12345"))
DEVICE = "e2e-check-01"


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        sys.exit(1)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def publish(password: str, messages: list[tuple[str, str]], username: str = DEVICE) -> None:
    async with aiomqtt.Client(MQTT_HOST, 1883, username=username, password=password, identifier=username) as c:
        for topic, payload in messages:
            await c.publish(topic, payload, qos=1)
            await asyncio.sleep(0.05)


async def main() -> None:
    async with httpx.AsyncClient(base_url=API, timeout=10) as http:
        r = await http.get("/api/v1/devices")
        check(r.status_code == 401, "unauthenticated request is rejected")

        r = await http.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
        check(r.status_code == 200 and r.json()["role"] == "admin", "admin login")

        await http.delete(f"/api/v1/devices/{DEVICE}", params={"purge_data": "true"})
        r = await http.post("/api/v1/devices", json={"device_id": DEVICE, "site": "test", "line": "l1"})
        check(r.status_code == 201, "device created with credentials")
        import asyncpg
        dbc = await asyncpg.connect(os.environ["DATABASE_URL"])
        row = await dbc.fetchrow("SELECT password_hash, kind FROM mqtt_accounts WHERE username = $1", DEVICE)
        acls = [r["topic"] for r in await dbc.fetch("SELECT topic FROM mqtt_acls WHERE username = $1", DEVICE)]
        check(row is not None and row["password_hash"].startswith("PBKDF2$sha512$") and row["kind"] == "device"
              and acls == [f"plc/+/+/{DEVICE}/+"], "device MQTT login stored (hashed) in the database")
        password = r.json()["credentials"]["password"]
        conn = r.json()["connection"]
        check(conn["telemetry_topic"] == f"plc/test/l1/{DEVICE}/telemetry", "connection info returned")

        rule = await http.post("/api/v1/alarm-rules", json={
            "name": "e2e high temp", "device_id": DEVICE, "tag": "temp", "rule_type": "high",
            "threshold": 80, "deadband": 2, "severity": "critical",
        })
        check(rule.status_code == 201, "alarm rule created")
        rule_id = rule.json()["id"]
        await asyncio.sleep(1)  # let the ingestor reload rules (NOTIFY)

        # Live stream: connect before publishing.
        cookie = http.cookies.get("plc_session")
        live_events: list[dict] = []

        async def listen() -> None:
            import websockets
            async with websockets.connect(
                API.replace("http", "ws") + f"/api/v1/stream?devices={DEVICE}",
                additional_headers={"Cookie": f"plc_session={cookie}"},
            ) as ws:
                async for raw in ws:
                    live_events.append(json.loads(raw))

        listener = asyncio.create_task(listen())
        await asyncio.sleep(0.5)

        topic = f"plc/test/l1/{DEVICE}/telemetry"
        base = {"schema_version": 1, "device_id": DEVICE}
        await publish(password, [
            (f"plc/test/l1/{DEVICE}/status", "online"),
            (topic, json.dumps({**base, "ts": now_iso(), "seq": 1, "status": "RUN",
                                "tags": {"temp": 70.5, "running": True, "recipe": "A"}})),
            (topic, json.dumps({**base, "ts": now_iso(), "seq": 2, "status": "RUN",
                                "tags": {"temp": 85.0, "running": True, "recipe": "A"}})),
            (topic, json.dumps({**base, "ts": now_iso(), "seq": 2, "status": "RUN",
                                "tags": {"temp": 85.0, "running": True, "recipe": "A"}})),  # duplicate
            (topic, '{"schema_version": 1, "device_id": "e2e-check-01", "tags": {"x": [1]}}'),  # invalid
            (f"plc/test/l1/someone-else/telemetry", json.dumps({**base, "device_id": "someone-else",
                                                                "tags": {"x": 1}})),  # ACL denied
        ])
        await asyncio.sleep(2)

        try:
            await publish("wrong-password", [(topic, "{}")])
            check(False, "wrong MQTT password is refused")
        except aiomqtt.MqttError:
            check(True, "wrong MQTT password is refused")

        r = await http.post(f"/api/v1/devices/{DEVICE}/credentials")
        check(r.status_code == 200, "rotate credentials of an existing device")
        old_password, password = password, r.json()["credentials"]["password"]
        await publish(password, [])
        check(True, "rotated password accepted by broker")
        await asyncio.sleep(6)  # the broker caches successful logins for 5 s
        try:
            await publish(old_password, [])
            check(False, "old password rejected after rotation")
        except aiomqtt.MqttError:
            check(True, "old password rejected after rotation")

        r = await http.get(f"/api/v1/devices/{DEVICE}")
        dev = r.json()
        tags = {t["tag"]: t for t in dev["tags"]}
        check(dev["online"] and dev["status"] == "RUN", "device online with status RUN")
        check(tags["temp"]["value"] == 85.0 and tags["running"]["value"] is True and tags["recipe"]["value"] == "A",
              "latest values have correct types")
        check(dev["msg_count"] == 2, f"duplicate dropped (msg_count={dev['msg_count']})")

        r = await http.get("/api/v1/devices")
        check(not any(d["device_id"] == "someone-else" for d in r.json()), "ACL blocked publish to foreign topic")

        r = await http.get(f"/api/v1/devices/{DEVICE}/history", params={"tags": "temp,running"})
        h = r.json()
        points = h["series"]["temp"]
        check(h["resolution"] == "raw" and min(p[2] for p in points) == 70.5 and max(p[3] for p in points) == 85.0,
              "history returns raw buckets with min/max envelope")

        r = await http.get(f"/api/v1/devices/{DEVICE}/export.csv", params={"tags": "temp,recipe"})
        check(r.status_code == 200 and r.text.count("\n") == 5, "CSV export")

        # The array-only format a PLC program may send instead of the canonical JSON.
        await publish(password, [(topic, "result: [275.500000,138.500000,4.170000, 6.140000,3.050000]")])
        await asyncio.sleep(1.5)
        tags = {t["tag"]: t["value"] for t in (await http.get(f"/api/v1/devices/{DEVICE}")).json()["tags"]}
        check([tags.get(f"result_{i}") for i in range(1, 6)] == [275.5, 138.5, 4.17, 6.14, 3.05],
              "'result: [..]' payload stored as result_1..result_5")
        raw = (await http.get("/api/v1/raw-messages", params={"device_id": DEVICE, "limit": 20})).json()
        check(raw[0]["payload"].startswith("result: [275.500000") and raw[0]["status"] == "ok"
              and "result_5=3.05" in raw[0]["detail"], "raw payload shown with parse result")
        check(any(m["status"] == "rejected" for m in raw) and any(m["status"] == "duplicate" for m in raw),
              "raw view includes rejected and duplicate messages")

        r = await http.get("/api/v1/system/ingest-errors")
        check(any(e["device_id"] == DEVICE for e in r.json()), "invalid payload recorded in ingest_errors")

        r = await http.get("/api/v1/alarms", params={"device_id": DEVICE})
        alarms = r.json()
        check(len(alarms) == 1 and alarms[0]["severity"] == "critical" and alarms[0]["trigger_value"] == 85.0,
              "high alarm raised")

        r = await http.post(f"/api/v1/alarms/{alarms[0]['id']}/ack", json={"comment": "checked"})
        check(r.status_code == 200 and r.json()["acked_by"] == ADMIN[0], "alarm acknowledged")

        await publish(password, [(topic, json.dumps({**base, "ts": now_iso(), "seq": 3, "tags": {"temp": 77.0}}))])
        await asyncio.sleep(1.5)
        r = await http.get("/api/v1/alarms", params={"device_id": DEVICE})
        check(r.json() == [], "alarm cleared below threshold - deadband")

        fault_rule = await http.post("/api/v1/alarm-rules", json={
            "name": "e2e fault", "device_id": DEVICE, "rule_type": "fault", "severity": "critical"})
        await asyncio.sleep(1)
        await publish(password, [(topic, json.dumps({**base, "ts": now_iso(), "seq": 4, "status": "FAULT",
                                                     "tags": {"temp": 50.0}}))])
        await asyncio.sleep(1.5)
        r = await http.get("/api/v1/alarms", params={"device_id": DEVICE})
        check("e2e fault" in [a["rule_name"] for a in r.json()], "status FAULT raises fault alarm")
        await http.delete(f"/api/v1/alarm-rules/{fault_rule.json()['id']}")

        types = {e.get("type") for e in live_events}
        check("live" in types and "alarm" in types, f"websocket delivered live + alarm events ({len(live_events)})")
        listener.cancel()

        await publish(password, [(f"plc/test/l1/{DEVICE}/status", "offline")])
        await asyncio.sleep(1)
        r = await http.get(f"/api/v1/devices/{DEVICE}")
        check(r.json()["online"] is False, "status 'offline' marks device offline")

        viewer_email = "e2e-viewer@example.com"
        users = (await http.get("/api/v1/users")).json()
        for u in users:
            if u["email"] == viewer_email:
                await http.delete(f"/api/v1/users/{u['id']}")
        r = await http.post("/api/v1/users", json={"email": viewer_email, "password": "viewer-pass", "role": "viewer"})
        viewer_id = r.json()["id"]
        async with httpx.AsyncClient(base_url=API, timeout=10) as vh:
            await vh.post("/api/v1/auth/login", json={"email": viewer_email, "password": "viewer-pass"})
            r = await vh.post(f"/api/v1/alarms/{alarms[0]['id']}/ack", json={})
            check(r.status_code == 403, "viewer cannot acknowledge alarms")
            r = await vh.patch(f"/api/v1/devices/{DEVICE}", json={"name": "x"})
            check(r.status_code == 403, "viewer cannot change config")

            # any user can change their own password; other sessions are signed out
            async with httpx.AsyncClient(base_url=API, timeout=10) as other:
                await other.post("/api/v1/auth/login", json={"email": viewer_email, "password": "viewer-pass"})
                r = await vh.post("/api/v1/auth/password", json={"current_password": "wrong", "new_password": "new-pass-123"})
                check(r.status_code == 400, "password change needs the current password")
                r = await vh.post("/api/v1/auth/password", json={"current_password": "viewer-pass", "new_password": "new-pass-123"})
                check(r.status_code == 200, "user changes own password")
                check((await vh.get("/api/v1/auth/me")).status_code == 200, "current session stays signed in")
                check((await other.get("/api/v1/auth/me")).status_code == 401, "other sessions are signed out")
            r = await http.post("/api/v1/auth/login", json={"email": viewer_email, "password": "viewer-pass"})
            check(r.status_code == 401, "old password no longer works")
            async with httpx.AsyncClient(base_url=API, timeout=10) as again:
                r = await again.post("/api/v1/auth/login", json={"email": viewer_email, "password": "new-pass-123"})
                check(r.status_code == 200, "new password works")
            await http.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})

        # ---- web simulator (separate site, own login, publishes under sim/)
        async with httpx.AsyncClient(base_url=SIM, timeout=15) as sim:
            check((await sim.get("/api/me")).status_code == 401, "simulator requires a session")
            async with httpx.AsyncClient(base_url=API, timeout=10) as vh2:
                await vh2.post("/api/v1/auth/login", json={"email": viewer_email, "password": "new-pass-123"})
                r = await sim.get("/api/me", headers={"Cookie": f"plc_session={vh2.cookies['plc_session']}"})
            check(r.status_code == 403, "non-admin cannot use the simulator")
            # single sign-on: the dashboard session cookie is accepted by the simulator
            sim.cookies.set("plc_session", http.cookies["plc_session"])
            check((await sim.get("/api/me")).json()["email"] == ADMIN[0], "dashboard session works in the simulator")
            await sim.delete("/api/devices")

            r = await sim.post("/api/send", json={"site": "simsite", "line": "l1", "device_id": "e2e-sim",
                                                  "template": "result: [275.500000,138.500000]"})
            check(r.status_code == 200 and r.json()["sent"][0]["topic"] == "sim/simsite/l1/e2e-sim/telemetry",
                  "simulator sends a single custom message")
            r = await sim.post("/api/send", json={"site": "simsite", "line": "l1", "device_id": DEVICE,
                                                  "template": "x: [1]"})
            check(r.status_code == 409, "simulator refuses a real PLC's device ID")
            r = await sim.post("/api/send", json={"site": "simsite", "line": "l2", "device_id": "e2e-bulk", "devices": 2,
                                                  "template": '{"t": {{rand:10:20}}, "n": {{counter}}}',
                                                  "count": 3, "interval_ms": 100})
            check(r.status_code == 200 and r.json()["job"]["total"] == 6, "bulk run started (2 devices x 3)")
            await asyncio.sleep(2.5)
            job = (await sim.get("/api/jobs")).json()[0]
            check(job["status"] == "done" and job["sent"] == 6, f"bulk run finished ({job['status']}, {job['sent']})")

            d = (await http.get("/api/v1/devices/e2e-sim")).json()
            vals = {t["tag"]: t["value"] for t in d["tags"]}
            check(d["simulated"] and vals == {"result_1": 275.5, "result_2": 138.5}, "simulated device on dashboard")
            d = (await http.get("/api/v1/devices/e2e-bulk-02")).json()
            check(d["simulated"] and d["msg_count"] == 3, "bulk values stored per device")
            r = await http.post("/api/v1/devices", json={"device_id": "e2e-sim"})
            check(r.status_code == 409, "dashboard cannot turn a simulated ID into a real device")

            # topic chosen by the admin
            r = await sim.post("/api/send", json={"site": "s", "line": "l", "device_id": "e2e-sim", "template": "x: [1]",
                                                  "topic": "plc/{{site}}/{{line}}/{{device}}/telemetry"})
            check(r.status_code == 409, "plc/ topic refused for a simulated device ID")
            r = await sim.post("/api/send", json={"site": "s", "line": "l", "device_id": "x", "template": "1",
                                                  "topic": "app/live/x"})
            check(r.status_code == 400, "internal app/ topics refused")
            r = await sim.post("/api/send", json={"site": "s", "line": "l", "device_id": "e2e-t", "template": "hello",
                                                  "topic": "test/{{device}}/raw"})
            check(r.status_code == 200 and r.json()["sent"][0]["topic"] == "test/e2e-t/raw", "test/ topic accepted")
            r = await sim.post("/api/send", json={"site": "e2esite", "line": "l9", "device_id": "e2e-plc-via-sim",
                                                  "template": "result: [7]",
                                                  "topic": "plc/{{site}}/{{line}}/{{device}}/telemetry"})
            check(r.status_code == 200, "plc/ topic accepted for a new device ID")
            await asyncio.sleep(1.5)
            d = (await http.get("/api/v1/devices/e2e-plc-via-sim")).json()
            check(d.get("simulated") is False and d["tags"][0]["value"] == 7, "plc/ topic behaves like a real PLC")
            await http.delete("/api/v1/devices/e2e-plc-via-sim", params={"purge_data": "true"})

            r = await sim.delete("/api/devices")
            check(r.json()["removed"] == 3, "remove all simulated devices")
            await asyncio.sleep(0.5)
            ids = [x["device_id"] for x in (await http.get("/api/v1/devices")).json()]
            check(not any(i.startswith("e2e-sim") or i.startswith("e2e-bulk") for i in ids),
                  "simulated devices gone from dashboard")

        # sign-out ends the shared session everywhere
        async with httpx.AsyncClient(base_url=API, timeout=10) as tmp:
            await tmp.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
            r = await tmp.post("/api/v1/auth/logout")
            check("plc_session=" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers.get("set-cookie", "")
                  or "max-age=0" in r.headers.get("set-cookie", "").lower(), "logout clears the session cookie")
            check((await tmp.get("/api/v1/auth/me")).status_code == 401, "signed out after logout")
        r = await http.get("/api/v1/system/logs")
        check(r.status_code in (200, 503), f"logs endpoint answers ({r.status_code})")

        # cleanup
        await http.delete(f"/api/v1/users/{viewer_id}")
        await http.delete(f"/api/v1/alarm-rules/{rule_id}")
        await http.delete(f"/api/v1/devices/{DEVICE}", params={"purge_data": "true"})
        check(await dbc.fetchval("SELECT count(*) FROM mqtt_accounts WHERE username = $1", DEVICE) == 0,
              "deleting the device removes its MQTT login")
        await dbc.close()
        print("ALL CHECKS PASSED")


asyncio.run(main())
