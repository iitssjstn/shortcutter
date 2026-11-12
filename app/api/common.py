"""Gedeelde helpers voor routes: templates, redirects en flash-meldingen."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.auth import ensure_csrf_cookie
from app.core.paths import fmt_bytes
from app.core.store import FRAMING_MODES
from app.estimation.estimator import fmt_views
from app.pipeline.run import ACTIVE_STATUSES, STATUS_LABELS

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


def _asset_version() -> str:
    """Korte hash van de statische bestanden, zodat browsers na een update de nieuwe CSS/JS laden."""
    import hashlib

    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    ACTIVE_STATUSES=ACTIVE_STATUSES,
    fmt_views=fmt_views,
    fmt_bytes=fmt_bytes,
    FRAMING_MODES=FRAMING_MODES,
    asset_version=_asset_version(),
)


def fmt_dt(dt) -> str:
    return dt.strftime("%d-%m-%Y %H:%M") if dt else "–"


def fmt_time(t: float | None) -> str:
    if t is None:
        return "–"
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:04.1f}"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["ts"] = fmt_time

FLASH_COOKIE = "sc_flash"


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
    ctx = dict(ctx or {})
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["request"] = request
    ctx["csrf_token"] = ensure_csrf_cookie(request)
    flash = None
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        try:
            flash = json.loads(unquote(raw))
        except ValueError:
            flash = None
    ctx.setdefault("flash", flash)
    resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if raw:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if msg:
        resp.set_cookie(
            FLASH_COOKIE, quote(json.dumps({"msg": msg, "kind": kind})), max_age=60, httponly=True, samesite="lax", path="/"
        )
    return resp


def safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"
