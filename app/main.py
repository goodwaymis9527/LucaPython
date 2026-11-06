from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, settings
from .routers import api, pages

app = FastAPI(
    title=settings.app_name,
    # /docs 留著：ALB 後面直接開 http://<alb-dns>/docs 就能測 API
    docs_url="/docs",
)

static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(pages.router)
app.include_router(api.router)
