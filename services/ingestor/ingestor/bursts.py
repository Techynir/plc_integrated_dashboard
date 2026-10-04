"""Poll times for gateways that send no timestamp and deliver their buffered polls in bursts.

The PM3032 gateway polls the PLC once per poll interval but publishes every ~5 s: all buffered
polls arrive within a few milliseconds, one message per register block per poll. Stamping each
message with its arrival time would put five polls in the same instant and leave 5 s holes, which
distorts lags, event timing and update-interval statistics.

So messages of that format are held per device until the burst is over (no message for
``QUIET_S``), then each message gets the time of the poll it came from: the n-th message of a
block within the burst is poll n, and polls are spaced one poll interval apart, the newest at the
arrival of the burst's first message (pauses inside a burst are sending time, not polling time).
A gateway that sends each poll on its own produces bursts of one, which keep their arrival time.
Times are exact to the gateway's send jitter (about 0.1 s), not to the millisecond.
"""

import datetime as dt
import json
from dataclasses import dataclass

from .parsing import Topic, is_modbus_blocks

QUIET_S = 0.3  # a burst is over after this much silence (gateway pauses inside a burst are < 0.15 s)


@dataclass
class Held:
    topic_str: str
    topic: Topic
    payload: bytes
    received_at: dt.datetime
    block: str  # register block address ("400002"), "" if the payload cannot be read


def block_key(payload: bytes) -> str | None:
    """The register block of a gateway message, "" for an unreadable one, None for other formats
    (those carry their own time or are stamped on arrival as before)."""
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not is_modbus_blocks(doc):
        return None
    return ",".join(str(b.get("full_addr", "")) for b in next(iter(doc.values())))


class BurstSpacer:
    def __init__(self, quiet_s: float = QUIET_S) -> None:
        self.quiet = dt.timedelta(seconds=quiet_s)
        self.pending: dict[str, list[Held]] = {}
        self.last_ts: dict[tuple[str, str], dt.datetime] = {}  # (device, block) -> last poll time given

    def add(self, held: Held) -> None:
        self.pending.setdefault(held.topic.device_id, []).append(held)

    def due(self, now: dt.datetime, poll_s: dict[str, float], force: bool = False) -> list[list[tuple[Held, dt.datetime]]]:
        """Bursts whose last message is at least QUIET_S old at ``now`` (all if ``force``), each as
        [(message, poll time)] in arrival order."""
        out = []
        for device, items in list(self.pending.items()):
            if force or now - items[-1].received_at >= self.quiet:
                del self.pending[device]
                out.append(self.space(device, items, poll_s.get(device, 1.0)))
        out.sort(key=lambda burst: burst[0][0].received_at)
        return out

    def space(self, device: str, items: list[Held], poll_s: float) -> list[tuple[Held, dt.datetime]]:
        counts: dict[str, int] = {}
        for h in items:
            counts[h.block] = counts.get(h.block, 0) + 1
        polls = max(counts.values())
        end = items[0].received_at
        step = dt.timedelta(seconds=poll_s)
        seen: dict[str, int] = {}
        out = []
        for h in items:
            n = seen.get(h.block, 0)
            seen[h.block] = n + 1
            ts = end - (polls - 1 - n) * step
            last = self.last_ts.get((device, h.block))
            if last is not None and ts <= last:  # never before (or on) the previous burst's last poll
                ts = last + dt.timedelta(milliseconds=1)
            self.last_ts[(device, h.block)] = ts
            out.append((h, ts))
        return out
