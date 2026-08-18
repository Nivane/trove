"""Server-Sent Events helpers (hand-rolled; no sse-starlette dependency).

Wire format per event:
    event: <type>
    data: <json>

"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


def sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    """Wrap an async iterator of {"type": str, "data": dict} into an SSE
    StreamingResponse (text/event-stream)."""

    async def body() -> AsyncIterator[str]:
        async for event in events:
            data = json.dumps(event["data"], ensure_ascii=False, default=str)
            yield f"event: {event['type']}\ndata: {data}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
