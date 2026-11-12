"""FastAPI-app: middleware, router-registratie en /healthz."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import crypto
from app.core.auth import CSRF_COOKIE, LoginRequired, csrf_protect, ensure_csrf_cookie
from app.core.config import Settings, get_settings, set_settings
from app.core.db import init_db, new_session
from app.core.setup import ensure_setup_code, has_users, reset_cache

log = logging.getLogger("shortcutter")

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
SETUP_EXEMPT = ("/setup", "/static/", "/healthz", "/favicon.ico")

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is not None:
        set_settings(settings)
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s.ensure_dirs()
    init_db(str(s.db_path))
    crypto.load_master_key(s)
    reset_cache()
    db = new_session()
    try:
        ensure_setup_code(db)
    finally:
        db.close()

    worker_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if s.run_worker:
            from app.worker.runner import Worker

            w = Worker()
            w.start()
            worker_holder["w"] = w
        yield
        if "w" in worker_holder:
            worker_holder["w"].stop()

    app = FastAPI(
        title="Shortcutter",
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def setup_and_headers(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/static/"):
            ensure_csrf_cookie(request)
        if not path.startswith(SETUP_EXEMPT):
            db = new_session()
            try:
                needs_setup = not has_users(db)
            finally:
                db.close()
            if needs_setup:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Setup nog niet voltooid"}, status_code=503)
                response = RedirectResponse("/setup", status_code=303)
                _set_csrf(request, response, s)
                return response
        response: Response = await call_next(request)
        _set_csrf(request, response, s)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Niet ingelogd"}, status_code=401)
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        nxt = request.url.path if request.method == "GET" else "/"
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        from app.api.common import render

        if request.url.path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if request.headers.get("hx-request"):
            return Response(
                f'<div class="flash flash-error">{_esc(str(exc.detail))}</div>',
                status_code=200,
                headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"},
                media_type="text/html",
            )
        titles = {403: "Geen toegang", 404: "Niet gevonden", 429: "Te veel verzoeken"}
        return render(
            request,
            "error.html",
            {"code": exc.status_code, "title": titles.get(exc.status_code, "Fout"), "detail": exc.detail},
            status_code=exc.status_code,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg")

    from app.api import account, auth, clips, downloads, keys, projects, settings as settings_api, stats, users

    for mod in (auth, projects, clips, downloads, stats, settings_api, keys, users, account):
        app.include_router(mod.router)
    return app


def _set_csrf(request: Request, response: Response, s: Settings) -> None:
    new_csrf = getattr(request.state, "new_csrf", None)
    if new_csrf and not request.cookies.get(CSRF_COOKIE):
        response.set_cookie(
            CSRF_COOKIE, new_csrf, httponly=True, samesite="lax", secure=s.cookie_secure, path="/",
            max_age=60 * 60 * 24 * 30,
        )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

