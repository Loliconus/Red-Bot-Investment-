"""HTTP-докачка пропущенных дельт перед обработкой live-сообщений."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from adapters.driving.web.security.session import require_session
from adapters.driving.web.ws.hub import BroadcastHub, ReplayGapError

router = APIRouter(prefix="/api/ws", tags=["websocket"], dependencies=[Depends(require_session)])


@router.get("/replay")
async def replay(
    request: Request,
    channel: str,
    since_seq: int = Query(ge=0),
) -> dict[str, Any]:
    hub: BroadcastHub = request.app.state.hub
    try:
        latest, messages = await hub.replay(channel, since_seq)
    except ReplayGapError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "История устарела; запросите snapshot",
                "latest_seq": exc.latest_seq,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "channel": channel,
        "latest_seq": latest,
        "events": [event.model_dump(mode="json") for event in messages],
    }
