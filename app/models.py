"""pydantic 模型（對應 Node 版 route 裡的輸入檢查）。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    body: str = Field("", max_length=2000)


class Item(BaseModel):
    id: int
    title: str
    body: str
    created_at: datetime


class HealthPayload(BaseModel):
    ok: bool
    db: dict
    error: Optional[str] = None
