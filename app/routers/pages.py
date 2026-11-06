"""頁面（對應 Node 版 app/page.jsx）。

ECS/Fargate 上不能快取：build 時的結果會被固定，之後換資料庫或改資料都會看到舊內容，
所以這裡每次請求都實際查 DB。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/")
def index(request: Request):
    items = []
    db_ok = False
    db_error = None
    try:
        items = db.list_items(50)
        db_ok = db.ping_db()
    except Exception as err:
        # 連不上資料庫時頁面要還能開（顯示錯誤而不是整個 500），health check 才不會誤判
        db_error = str(err)

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "items": items, "info": db.db_info(), "db_ok": db_ok, "db_error": db_error},
    )


@router.get("/readyz")
def readyz():
    """給部署腳當「鏡像跑起來了沒有」的簡單探測（不是 target group 用的那條）。"""
    return {"app": "luca-python", "version": "1.0.0"}
