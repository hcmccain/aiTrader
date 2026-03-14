import asyncio
import json
import logging
import time
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

_subscribers: list[asyncio.Queue] = []


def publish_event(event: dict):
    """Publish an event to all connected SSE clients (thread-safe)."""
    event["timestamp"] = time.time()
    data = json.dumps(event, default=str)
    stale = []
    for q in _subscribers:
        try:
            q.put_nowait(data)
        except Exception:
            stale.append(q)
    for q in stale:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


async def subscribe() -> AsyncGenerator[str, None]:
    """Yield SSE-formatted messages for a single client."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.append(q)
    try:
        while True:
            data = await q.get()
            yield data
    except asyncio.CancelledError:
        pass
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass
