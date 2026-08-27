from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import DB_PATH, init_db
from routers import (
    admin_web,
    admin_web_access,
    admin_web_write,
    departments,
    me,
    me_web,
    models,
    openwebui,
    sync,
    users,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(title="Firdi LiteLLM Admin API", lifespan=lifespan)

app.include_router(departments.router)
app.include_router(users.router)
app.include_router(models.router)
app.include_router(sync.router)
app.include_router(openwebui.router)
app.include_router(me.router)
app.include_router(me_web.router)
app.include_router(admin_web.router)
app.include_router(admin_web_write.router)
app.include_router(admin_web_access.router)


@app.exception_handler(StarletteHTTPException)
async def admin_web_html_errors(request, exc: StarletteHTTPException):
    """admin-web 頁面的錯誤一律轉成 HTML（R-43）；其他路徑維持 FastAPI 預設的 JSON。

    只在 request path 落在 admin_web.PREFIX 底下才接管，避免動到
    /api/v1/departments、/api/v1/models 等既有 curl 端點的 JSON 錯誤格式
    （階段 01 已驗證過的 409/422 等回應不能變）。
    """
    if request.url.path.startswith(admin_web.PREFIX):
        return admin_web.render_error_page(exc)
    return await http_exception_handler(request, exc)


@app.get("/health")
def health():
    return {"status": "ok"}
