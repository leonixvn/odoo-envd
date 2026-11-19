"""SSE event stream: job/env lifecycle for the GUI to subscribe to."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from envd.api.deps import get_store, require_scope
from envd.core.state import Store

router = APIRouter(tags=["events"])


@router.get("/events")
def events(
    last_id: int = Query(default=0, ge=0),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> StreamingResponse:
    return StreamingResponse(
        _stream(store, last_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream(store: Store, last_id: int):
    cursor = last_id
    idle = 0
    while idle < 30:
        batch = store.events_after(cursor, limit=100)
        if not batch:
            # heartbeat keeps intermediaries from closing the connection
            yield ": keepalive\n\n"
            idle += 1
            await asyncio.sleep(1)
            continue
        idle = 0
        for event in batch:
            cursor = event["id"]
            payload = json.dumps(event["payload"], default=str)
            yield f"id: {event['id']}\nevent: {event['kind']}\ndata: {payload}\n\n"
