"""Backend web: página inicial, webhook de pagamentos e painel administrativo.

Execução local:  uvicorn app:app --reload
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from admin.auth import NotAuthenticated
from admin.routes import router as admin_router
from config import BASE_DIR, configure_logging, settings
from database import check_connection
from payments import PaymentClient, get_payment_client
from services import payment_service

configure_logging()
settings.require_web()
logger = logging.getLogger("app")

app = FastAPI(
    title="Telegram Vendas",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="tv_admin_session",
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith(("/docs", "/openapi.json")):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://*.mlstatic.com; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        )
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.include_router(admin_router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"telegram_link": settings.telegram_link}
    )


@app.get("/health", include_in_schema=False)
def health():
    ok = check_connection()
    return JSONResponse({"status": "ok" if ok else "error", "database": ok}, status_code=200 if ok else 503)


@app.post("/webhooks/mercadopago", include_in_schema=False)
async def mercadopago_webhook(request: Request, client: PaymentClient = Depends(get_payment_client)):
    body = await request.body()
    if len(body) > 64 * 1024:
        return PlainTextResponse("payload too large", status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    query = dict(request.query_params)
    result = await run_in_threadpool(
        payment_service.handle_mercadopago_webhook, headers=headers, query=query, body=body, client=client
    )
    return PlainTextResponse(result.message, status_code=result.status_code)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /admin\n")
