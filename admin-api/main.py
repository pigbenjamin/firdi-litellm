from contextlib import asynccontextmanager

from fastapi import FastAPI

from database import DB_PATH, init_db
from routers import departments, me, me_web, models, openwebui, sync, users


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


@app.get("/health")
def health():
    return {"status": "ok"}
