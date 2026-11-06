from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from routers.check_price import router as price_router
from routers.check_sms import router as check_router
from routers.cancel_sms import router as cancel_router
from routers.order_sms import close_session, get_session, router as order_router
from routers.retry_sms import router as retry_router
from routers.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = get_session()
    print("[START] apisms siap menerima order", flush=True)
    yield
    await close_session()
    print("[STOP] session provider ditutup", flush=True)


app = FastAPI(title="apisms", lifespan=lifespan)
app.include_router(order_router)
app.include_router(check_router)
app.include_router(price_router)
app.include_router(retry_router)
app.include_router(cancel_router)
app.include_router(webhook_router)


@app.get("/")
async def root() -> dict:
    return {"status": True, "service": "apisms"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
