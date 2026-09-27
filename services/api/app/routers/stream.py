import asyncio
import json

from fastapi import APIRouter, WebSocket

from ..broker import Subscriber, link
from ..deps import websocket_user

router = APIRouter()


def _parse_devices(value) -> set[str] | None:
    if value in (None, "", "*"):
        return None
    if isinstance(value, str):
        value = value.split(",")
    return {str(v) for v in value if v}


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """Live events. Optional ?devices=a,b filter; the client may later send {"devices": [...]}."""
    if await websocket_user(websocket) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    sub = Subscriber(devices=_parse_devices(websocket.query_params.get("devices")))
    link.hub.add(sub)
    if link.hub.ingestor_stats:
        sub.offer(json.dumps(link.hub.ingestor_stats))

    async def sender() -> None:
        while True:
            await websocket.send_text(await sub.queue.get())

    async def receiver() -> None:
        while True:
            try:
                msg = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and "devices" in msg:
                sub.devices = _parse_devices(msg["devices"])

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        # Either side failing (normally a client disconnect) ends the session.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.exception()
    finally:
        for task in tasks:
            task.cancel()
        link.hub.remove(sub)
