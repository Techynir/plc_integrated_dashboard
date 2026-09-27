"""Application logs for admins, read from Google Cloud Logging.

On GCP every container's output is shipped by Docker's gcplogs driver
(deploy/compose.gcp.yml). The API reads it back with the VM's service account
(roles/logging.viewer), using the metadata server for credentials, so no keys
are involved. Locally (no metadata server) the endpoint reports that logs are
only collected on GCP.
"""

import datetime as dt
import json
import re
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import CurrentUser, admin

router = APIRouter(prefix="/system/logs", tags=["logs"])

METADATA = "http://metadata.google.internal/computeMetadata/v1"
COMPOSE_PROJECT = "plc-integrated-dashboard"
COMPONENTS = ["api", "ingestor", "mosquitto", "web", "simulator-ui", "db", "init", "certs"]
RANGES = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440, "7d": 10080, "30d": 43200}

# Cloud Logging does not pass backslash escapes through to its regex engine, so these
# patterns (and those built below) contain no backslashes: [.] instead of \. and no \b.
ERROR_RE = '(?i)("severity": ?"(ERROR|CRITICAL)"|"level": ?"(error|fatal|panic)"|error|exception|traceback|fatal)'
WARN_RE = '(?i)("severity": ?"WARNING"|"level": ?"warn|warn)'

_token: dict = {"value": None, "expires": 0.0, "project": None}


class LogsUnavailable(Exception):
    pass


async def _credentials(client: httpx.AsyncClient) -> tuple[str, str]:
    if _token["value"] and _token["expires"] - 60 > time.time():
        return _token["value"], _token["project"]
    headers = {"Metadata-Flavor": "Google"}
    try:
        tok = await client.get(f"{METADATA}/instance/service-accounts/default/token", headers=headers, timeout=3)
        project = await client.get(f"{METADATA}/project/project-id", headers=headers, timeout=3)
        tok.raise_for_status()
        project.raise_for_status()
    except httpx.HTTPError as exc:
        raise LogsUnavailable("Logs are collected in Google Cloud Logging when running on GCP.") from exc
    data = tok.json()
    _token.update(value=data["access_token"], expires=time.time() + data.get("expires_in", 300), project=project.text)
    return _token["value"], _token["project"]


def regex_literal(text: str) -> str:
    """Regex matching `text` literally without using backslashes (see note above)."""
    out = []
    for ch in text:
        if ch.isalnum() or ch in " _-:@/,=":
            out.append(ch)
        elif ch in "\\^]":
            out.append(".")  # cannot be expressed without a backslash; match any character
        else:
            out.append(f"[{ch}]")
    return "".join(out)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_filter(project: str, component: str | None, q: str, level: str, start: dt.datetime, end: dt.datetime) -> str:
    """Cloud Logging query. q is free text, or key=value / key:value to match a field of JSON log lines."""
    parts = [
        f'logName="projects/{project}/logs/gcplogs-docker-driver"',
        f'timestamp>="{start.isoformat()}"',
        f'timestamp<="{end.isoformat()}"',
    ]
    names = [component] if component else COMPONENTS
    parts.append(
        "jsonPayload.container.name=~"
        + _quote(f"^/{COMPOSE_PROJECT}-({'|'.join(names)})-[0-9]+$")  # names come from COMPONENTS
    )
    q = q.strip()
    kv = re.fullmatch(r"([A-Za-z0-9_.-]{1,64})\s*[=:]\s*(.+)", q)
    if kv:
        key, value = kv.group(1), kv.group(2).strip().strip('"')
        # "key": "value" (JSON logs) or key=value (plain logs)
        pattern = f'(?i)("{regex_literal(key)}": ?"?{regex_literal(value)}|{regex_literal(key)}={regex_literal(value)})'
        parts.append(f"jsonPayload.message=~{_quote(pattern)}")
    elif q:
        parts.append(f"jsonPayload.message:{_quote(q)}")
    if level == "error":
        parts.append(f"jsonPayload.message=~{_quote(ERROR_RE)}")
    elif level == "warning":
        parts.append(f"(jsonPayload.message=~{_quote(ERROR_RE)} OR jsonPayload.message=~{_quote(WARN_RE)})")
    return " AND ".join(parts)


def classify(message: str) -> str:
    if re.search(ERROR_RE, message):
        return "error"
    if re.search(WARN_RE, message):
        return "warning"
    return "info"


def to_entry(raw: dict) -> dict:
    payload = raw.get("jsonPayload", {})
    message = payload.get("message", "")
    name = payload.get("container", {}).get("name", "")
    m = re.match(rf"^/{COMPOSE_PROJECT}-(.+)-[0-9]+$", name)
    parsed = None
    if message.startswith("{"):
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            parsed = None
    return {
        "ts": raw.get("timestamp"),
        "component": m.group(1) if m else name.lstrip("/"),
        "level": classify(message),
        "message": message,
        "fields": parsed if isinstance(parsed, dict) else None,
    }


@router.get("")
async def read_logs(
    component: str | None = Query(None, description="one of the components, empty = all"),
    q: str = Query("", max_length=200, description="text, or key=value for JSON log fields"),
    level: Literal["all", "warning", "error"] = "all",
    range_: str = Query("1h", alias="range"),
    page_token: str | None = None,
    limit: int = Query(100, ge=10, le=500),
    _: CurrentUser = Depends(admin),
) -> dict:
    if component and component not in COMPONENTS:
        raise HTTPException(400, f"Unknown component; choose one of {', '.join(COMPONENTS)}")
    if range_ not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=RANGES[range_])
    async with httpx.AsyncClient() as client:
        try:
            token, project = await _credentials(client)
        except LogsUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        body = {
            "resourceNames": [f"projects/{project}"],
            "filter": build_filter(project, component, q, level, start, end),
            "orderBy": "timestamp desc",
            "pageSize": limit,
        }
        if page_token:
            body["pageToken"] = page_token
        resp = await client.post(
            "https://logging.googleapis.com/v2/entries:list",
            json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20,
        )
    if resp.status_code == 429:
        raise HTTPException(429, "Cloud Logging read quota reached; wait a minute and try again")
    if resp.status_code >= 400:
        detail = resp.json().get("error", {}).get("message", resp.text[:200]) if resp.content else resp.status_code
        raise HTTPException(502, f"Cloud Logging error: {detail}")
    data = resp.json()
    return {
        "entries": [to_entry(e) for e in data.get("entries", [])],
        "next_page_token": data.get("nextPageToken"),
        "components": COMPONENTS,
    }


@router.get("/components")
async def components(_: CurrentUser = Depends(admin)) -> list[str]:
    return COMPONENTS
