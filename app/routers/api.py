"""/api/health 與 /api/items（對應 Node 版 app/api/*/route.js）。"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from .. import db
from ..models import Item, ItemCreate

router = APIRouter(prefix="/api")


def client_origin(request: Request) -> str:
    """ALB / 反向代理後面：request.url 的 base 是「容器自己看到的」 0.0.0.0:8000，
    直接用它導向會讓瀏覽器連 http://0.0.0.0:8000/。
    導向的 base 要用 client 實際看到的來源：x-forwarded-host → host → referer，都沒才退回 url。
    """
    proto = request.headers.get("x-forwarded-proto") or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        return f"{proto}://{host}"
    referer = request.headers.get("referer")
    if referer:
        try:
            parsed = urlsplit(referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            pass  # 不合法的 referer 就忽略，退回 url
    return f"{proto}://{request.headers.get('host', '')}" or str(request.url.rsplit("/", 1)[0])


@router.get("/health")
def health():
    """ECS 的 target group / health check 打的就是這條。

    health check 若回 500，ECS 會把任務當成 unhealthy 而一直重啟；
    這裡一律回 200，把 DB 狀態寫在 body，讓 ALB 只看「服務活得過來」，DB 問題由內容判斷。
    """
    db_ok = False
    db_error = None
    try:
        db_ok = db.ping_db()
    except Exception as err:  # psycopg 的異常層級很深，一律捕捉才不會 500
        db_error = str(err)

    return JSONResponse({"ok": db_ok, "db": db.db_info(), "error": db_error}, status_code=200)


@router.get("/items")
def items(limit: int = 50):
    # created_at 是 datetime：JSONResponse 直接送 dict 會 TypeError，所以先過 pydantic 轉 ISO 字串
    rows = [Item.model_validate(row).model_dump(mode="json") for row in db.list_items(limit)]
    return JSONResponse(rows, status_code=200)


@router.post("/items")
async def create_item(request: Request):
    """表單送出是 form-encoded，fetch 送出是 JSON，兩種都支援。"""
    content_type = request.headers.get("content-type") or ""
    title = ""
    body = ""

    if "application/json" in content_type:
        payload = await request.json()
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "")
    else:
        form = await request.form()
        title = str(form.get("title") or "").strip()
        body = str(form.get("body") or "")

    try:
        item_in = ItemCreate(title=title, body=body)
    except ValidationError as err:
        errors = err.errors()
        return JSONResponse({"error": (errors[0].get("msg") if errors else str(err))}, status_code=400)

    # pydantic 負責把 created_at（datetime）轉成 ISO 字串
    item = Item.model_validate(db.add_item(item_in.title, item_in.body)).model_dump(mode="json")

    # 表單提交要 303，讓瀏覽器改用 GET 回主页（避免重新整理重複新增）
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return RedirectResponse(f"{client_origin(request)}/", status_code=303)

    return JSONResponse(item, status_code=201)
