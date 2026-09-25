"""Единый формат сообщений WS для всех каналов."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field


class Envelope(BaseModel):
    channel: str
    seq: int = Field(ge=0)
    ts: AwareDatetime
    type: str
    payload: dict[str, Any] | list[Any]

    @classmethod
    def snapshot(
        cls, channel: str, seq: int, ts: datetime, payload: dict[str, Any] | list[Any]
    ) -> Envelope:
        return cls(channel=channel, seq=seq, ts=ts, type="snapshot", payload=payload)
