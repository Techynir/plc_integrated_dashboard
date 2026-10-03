# PLC Integration Guide

How to connect a PLC to the dashboard. The PLC publishes JSON over MQTT with TLS; the cloud side stores it, displays it and raises alarms.

## 1. What you will receive

An admin creates the device in the dashboard (**Admin → Devices → Add device**) and gives you:

| Item | Example |
|---|---|
| Broker host | `34-100-10-20.sslip.io` (or the company domain) |
| Port | `8883` (MQTT over TLS 1.2+) |
| CA certificate | `plc-dashboard-ca.crt` (from **Admin → Devices → Download CA certificate**) |
| Client ID | `plc-filler-01` |
| Username | `plc-filler-01` (always equal to the device ID) |
| Password | shown once to the admin; ask for a rotation if it is lost |
| Telemetry topic | `plc/pune/line1/plc-filler-01/telemetry` |
| Status topic | `plc/pune/line1/plc-filler-01/status` |

## 2. MQTT connection settings

| Setting | Value |
|---|---|
| Protocol | MQTT 3.1.1 or 5.0 |
| TLS | Enabled, verify the server with the provided CA certificate. No client certificate is needed. |
| Clean session | `false` (persistent session), so QoS 1 messages are not lost during short disconnects |
| Keep-alive | 30–60 s |
| **Last Will** | topic = status topic, payload `offline`, QoS 1, **retain = true** |
| After connecting | publish `online` to the status topic, QoS 1, **retain = true** |

The device may publish **only** to topics under `plc/<site>/<line>/<its device ID>/`. It cannot subscribe to anything. Publishing elsewhere is silently dropped by the broker.

## 3. Telemetry message

Publish to the telemetry topic with **QoS 1**, typically once per second (the admin configures the expected interval so the dashboard can detect when data stops).

```json
{
  "schema_version": 1,
  "device_id": "plc-filler-01",
  "ts": "2026-09-26T10:15:30.123Z",
  "seq": 102934,
  "status": "RUN",
  "tags": {
    "speed_bpm": 118.5,
    "fill_volume_ml": 500.8,
    "bottle_count": 18234,
    "door_closed": true,
    "recipe": "COLA-500"
  },
  "quality": {
    "fill_volume_ml": "UNCERTAIN"
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `schema_version` | yes | Always `1` |
| `device_id` | yes | Must equal the device ID in the topic and the MQTT username |
| `ts` | recommended | UTC, RFC 3339 (`2026-09-26T10:15:30.123Z`). If missing, or more than 5 minutes off, the server time is used, so keep the PLC clock NTP-synced. |
| `seq` | recommended | Counter incremented on every message. Lets the server drop duplicates and count lost messages. It may restart from 0 after a PLC restart. |
| `status` | optional | `RUN`, `IDLE`, `STOP`, `FAULT` or `MAINT`. `FAULT` can raise an alarm. |
| `tags` | yes | 1–500 values. Names: letters, digits, `_ . -`, up to 64 characters. Values: number, `true`/`false`, or text (≤ 256 characters). |
| `quality` | optional | Per-tag `GOOD` / `UNCERTAIN` / `BAD`. Omitted tags are `GOOD`. `BAD` values are stored but do not trigger alarms. |

Rules worth knowing:

* Send **integers** for counters and codes (`18234`, not `18234.0`) so they display without decimals.
* Send **raw engineering values** where possible. If the PLC can only send raw counts (e.g. 0–27648), the admin can set a scale and offset per tag in the dashboard.
* Keep tag names **stable**; a renamed tag appears as a new tag and loses its history link.
* `NaN`/`Infinity` are not valid JSON and the message is rejected.
* Maximum message size is 64 KB.
* New tags appear in the dashboard automatically; no server change is needed.

Complete examples: [`schemas/examples/`](../schemas/examples/). Formal JSON Schema: [`schemas/telemetry.v1.json`](../schemas/telemetry.v1.json).

### 3a. Simpler formats (also accepted)

If the PLC program cannot build the JSON above, it may send a plain list of values instead. Values are named by position:

| Payload sent | Values shown on the dashboard |
|---|---|
| `result: [275.500000,138.500000,4.170000,6.140000,3.050000]` | `result_1` = 275.5, `result_2` = 138.5, … `result_5` = 3.05 |
| `{"result": [275.5, 138.5, 4.17]}` | `result_1`, `result_2`, `result_3` |
| `{"temp": 21.5, "running": true}` | `temp`, `running` |
| `[275.5, 138.5]` | `value_1`, `value_2` |

With these formats the server's receive time is used as the timestamp, and duplicates cannot be detected (there is no `seq`). **Keep the order of the values fixed**: `result_3` always means "the third value". The admin gives each position a readable name and unit in the dashboard (device page → All tags → Configure).

Every message, accepted or rejected, is visible exactly as received on the device page under **Raw data from PLC** (kept 7 days). This is the quickest way to check what the PLC is actually sending.

## 4. Testing without the PLC

With `mosquitto_pub` (from the Mosquitto package) and the CA file:

```bash
mosquitto_pub -h <host> -p 8883 --cafile plc-dashboard-ca.crt \
  -u plc-filler-01 -P '<password>' -i plc-filler-01 -q 1 \
  -t plc/pune/line1/plc-filler-01/telemetry \
  -m '{"schema_version":1,"device_id":"plc-filler-01","tags":{"speed_bpm":118.5}}'
```

Or run the bundled simulator as that device:

```bash
cd simulator && pip install -r requirements.txt
python simulate.py --device-id plc-filler-01 --password '<password>' \
  --host <host> --port 8883 --ca plc-dashboard-ca.crt --site pune --line line1
```

The value should appear on the dashboard within about a second.

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Connection refused: *not authorised* | Wrong username/password, or the device is disabled in the dashboard |
| TLS handshake fails | CA certificate not loaded on the PLC, or the PLC connects by a host name that is not in the certificate (use exactly the host name you were given) |
| Connected but nothing appears | Topic does not match `plc/<site>/<line>/<device ID>/telemetry`, or the device ID segment differs from the username |
| Messages appear under **System health → Rejected messages** | Payload does not match the schema; the reason column says which field |
| Device flips to offline | No telemetry for longer than 3× the expected interval (min. 15 s), or the Last Will fired |
