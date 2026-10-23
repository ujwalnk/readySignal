"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .assets import MAX_UPLOAD_BYTES
from .crypto import KDF_DEFAULT, SecretError
from .engine import FILE_NAMES, AmbiguousTime, ConfigError, Conflict, Engine, NotFound, PartialFailure, ValidationFailed, backup_blob
from .models import iso, utcnow
from .notify import PLACEHOLDERS
from .store import StoreError

log = logging.getLogger("countdown")
STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "csrf_token"
APP_NAME = "Ready Signal"
APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'"
)
APP_FRAMING = "; frame-ancestors 'none'"  # the widget omits the directive: any parent page may embed it


class AccountIn(BaseModel):
    name: str
    icon: str = "⏳"
    duration_seconds: int
    project: str = ""
    prompt: str = ""
    group: str = ""


class ExpiryIn(BaseModel):
    """Local wall-clock time in the effective time zone (or an ISO string with an explicit offset)."""

    completes_at: str | None = None
    occurrence: str | None = None  # "earlier"/"later": only needed when the local time occurs twice
    complete_now: bool = False


class StartIn(BaseModel):
    mode: str = "default"  # "default" | "at"
    completes_at: str | None = None
    occurrence: str | None = None
    project: str = ""  # empty == keep the saved value
    prompt: str = ""


class ReminderIn(BaseModel):
    time: str
    project: str = ""  # empty == keep the saved value
    prompt: str = ""


class StatsIn(BaseModel):
    enabled: bool
    confirm: bool = False


class TemplateIn(BaseModel):
    template: str = Field(max_length=100_000)


async def csrf_guard(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie, header = request.cookies.get(COOKIE, ""), request.headers.get("x-csrf-token", "")
    if not cookie or not hmac.compare_digest(cookie, header):
        raise HTTPException(403, "CSRF token missing or invalid")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request rejected")


def read_secret() -> str | None:
    """SECRET from the environment, or from the file named by SECRET_FILE (Docker/Compose secrets)."""
    value = os.environ.get("SECRET")
    path = os.environ.get("SECRET_FILE")
    if value is None and path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise SecretError("SECRET_FILE could not be read") from None
    return value


def create_app(
    data_dir: str | None = None, secret: str | None = None, start_scheduler: bool = True, clock: Any = utcnow
) -> FastAPI:
    data = data_dir or os.environ.get("DATA_DIR", "/data")
    engine = Engine(data, secret if secret is not None else read_secret(), clock)  # SecretError -> no start
    engine.load()  # StoreError -> no start; nothing is overwritten

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if start_scheduler:
            task = asyncio.create_task(engine.run(), name="scheduler")  # first tick == restart recovery
        yield
        engine.stop()
        if task:
            await task

    app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    api = APIRouter(prefix="/api", dependencies=[Depends(csrf_guard)])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/widget"):
            # The widget is meant to be embedded by a dashboard on the private network: any parent may frame it.
            h.setdefault("Content-Security-Policy", APP_CSP)  # no frame-ancestors, no X-Frame-Options
            h.setdefault("Cache-Control", "no-store")
        else:
            h.setdefault("Content-Security-Policy", APP_CSP + APP_FRAMING)
            h.setdefault("X-Frame-Options", "DENY")
        return resp

    @app.exception_handler(AmbiguousTime)
    async def _a(_: Request, exc: AmbiguousTime) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "ambiguous_time"}, 422)

    @app.exception_handler(ValidationFailed)
    async def _v(_: Request, exc: ValidationFailed) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, 422)

    @app.exception_handler(NotFound)
    async def _n(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(Conflict)
    async def _c(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(PartialFailure)
    async def _p(_: Request, exc: PartialFailure) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "partial"}, 500)

    @app.exception_handler(StoreError)
    async def _s(_: Request, exc: StoreError) -> JSONResponse:
        log.error("storage error: %s", exc)
        return JSONResponse({"detail": f"storage error: {exc}"}, 500)

    @app.get("/healthz")
    async def healthz() -> Response:
        ok = engine.scheduler_running or not start_scheduler
        body = {"status": "ok" if ok else "starting", "scheduler": ok}
        return JSONResponse(body, 200 if ok else 503)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        token = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("__THEME__", engine.config["theme"]).replace("__CSRF__", token).replace("__APP__", APP_NAME)
        resp = HTMLResponse(html, headers={"Cache-Control": "no-store"})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=os.environ.get("COOKIE_SECURE") == "1")
        return resp

    def snapshot_payload() -> dict[str, Any]:
        return {
            "now": iso(engine.clock()),
            "accounts": engine.list_accounts(),
            "config": engine.public_config(),
            "analytics_error": engine.analytics_error,
            "placeholders": list(PLACEHOLDERS),
            "public_base_url": os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        }

    @api.get("/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        return snapshot_payload()

    @api.get("/accounts")
    async def accounts() -> dict[str, Any]:
        return {"now": iso(engine.clock()), "accounts": engine.list_accounts()}

    @api.post("/accounts", status_code=201)
    async def create(body: AccountIn) -> dict[str, Any]:
        return await engine.create_account(body.model_dump())

    @api.put("/accounts/{account_id}")
    async def update(account_id: str, body: AccountIn) -> dict[str, Any]:
        return await engine.update_account(account_id, body.model_dump())

    @api.post("/accounts/{account_id}/start")
    async def start(account_id: str) -> dict[str, Any]:
        return await engine.start(account_id)

    @api.post("/accounts/{account_id}/cooldown")
    async def cooldown(account_id: str, body: StartIn) -> dict[str, Any]:
        """Main-site Start cooldown dialog: default duration or explicit completion time, optional project/prompt."""
        return await engine.begin_cooldown(
            account_id, body.mode, body.completes_at, body.occurrence, body.project, body.prompt
        )

    @api.post("/accounts/{account_id}/reminder", status_code=201)
    async def reminder(account_id: str, body: ReminderIn) -> dict[str, Any]:
        return await engine.schedule_reminder(account_id, body.time, body.project, body.prompt)

    @api.put("/accounts/{account_id}/expiry")
    async def set_expiry(account_id: str, body: ExpiryIn) -> dict[str, Any]:
        """Edit when a running cooldown completes (or finish it now). Account metadata is a separate operation."""
        return await engine.set_expiry(account_id, body.completes_at, body.occurrence, body.complete_now)

    @api.put("/accounts/{account_id}/icon")
    async def set_icon(account_id: str, request: Request) -> dict[str, Any]:
        """Raw image bytes as the request body (no multipart dependency); validated and sanitised server-side."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"image is too large (limit {MAX_UPLOAD_BYTES // 1024} KB)")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"image is too large (limit {MAX_UPLOAD_BYTES // 1024} KB)")
            chunks.append(chunk)
        return await engine.set_icon(account_id, b"".join(chunks))

    @api.delete("/accounts/{account_id}/icon")
    async def delete_icon(account_id: str) -> dict[str, Any]:
        return await engine.clear_icon(account_id)

    @api.post("/accounts/{account_id}/archive")
    async def archive(account_id: str) -> dict[str, Any]:
        return await engine.set_archived(account_id, True)

    @api.post("/accounts/{account_id}/restore")
    async def restore(account_id: str) -> dict[str, Any]:
        return await engine.set_archived(account_id, False)

    @api.put("/settings")
    async def settings(patch: dict[str, Any]) -> dict[str, Any]:
        return await engine.update_settings(patch)

    @api.post("/settings/template/preview")
    async def preview(body: TemplateIn) -> dict[str, Any]:
        return engine.preview(body.template)

    @api.post("/settings/slack/test")
    async def slack_test() -> dict[str, Any]:
        return await engine.send_test()

    @api.post("/settings/statistics")
    async def statistics(body: StatsIn) -> dict[str, Any]:
        return await engine.set_statistics(body.enabled, body.confirm)

    @api.get("/stats")
    async def stats() -> dict[str, Any]:
        return engine.stats()

    @api.get("/stats/graphs")
    async def stats_graphs(
        date_from: Annotated[str | None, Query(alias="from")] = None,
        date_to: Annotated[str | None, Query(alias="to")] = None,
        group: str = "day",
        account: Annotated[list[str] | None, Query()] = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        return engine.graphs(date_from=date_from, date_to=date_to, group=group, accounts=account, project=project)

    @api.get("/notifications")
    async def notifications() -> dict[str, Any]:
        return {"jobs": engine.list_jobs()}

    @api.post("/notifications/{job_id}/retry")
    async def retry(job_id: str) -> dict[str, str]:
        await engine.retry_job(job_id)
        return {"status": "queued"}

    @api.get("/encryption")
    async def encryption() -> dict[str, Any]:
        return {
            "algorithm": "AES-256-GCM",
            "kdf": {k: v for k, v in KDF_DEFAULT.items()},
            "files": {n: (engine.data_dir / f).exists() for n, f in FILE_NAMES.items()},
            "secret": "provided via environment (never displayed)",
        }

    @api.get("/backup")
    async def backup() -> Response:
        return Response(
            backup_blob(engine.data_dir),
            media_type="application/gzip",
            headers={"Content-Disposition": 'attachment; filename="ready-signal-backup-encrypted.tar.gz"'},
        )

    # ------------------------------------------------------------ images
    @app.get("/assets/icons/{asset_id}")
    async def icon(asset_id: str) -> Response:
        data, ctype = engine.read_icon(asset_id)  # NotFound -> 404; only ids referenced by an account resolve
        return Response(
            data,
            media_type=ctype,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",  # ids change on replacement
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "Content-Disposition": "inline",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    # ------------------------------------------------------------ widget (iframe)
    def widget_enabled() -> None:
        if not engine.config["widget"]["enabled"]:
            raise HTTPException(404, "the widget is not enabled")

    # No credential by design: the widget is an embedded view on a private deployment (docs/widget.md).
    wapi = APIRouter(prefix="/widget/api", dependencies=[Depends(widget_enabled)])

    @wapi.get("/summary")
    async def widget_summary() -> dict[str, Any]:
        return engine.widget_summary()

    @wapi.post("/accounts/{account_id}/start")
    async def widget_start(account_id: str) -> dict[str, Any]:
        return await engine.widget_start(account_id)

    @wapi.get("/accounts/{account_id}/prompt")
    async def widget_prompt(account_id: str) -> dict[str, str]:
        return {"prompt": engine.widget_prompt(account_id)}

    @app.get("/widget", response_class=HTMLResponse, dependencies=[Depends(widget_enabled)])
    async def widget_page() -> HTMLResponse:
        html = (STATIC / "widget.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__THEME__", engine.config["theme"]).replace("__APP__", APP_NAME))

    app.include_router(wapi)
    app.include_router(api)
    def _favicon() -> Response:
        return Response(
            (STATIC / "favicon.svg").read_bytes(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/favicon", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return _favicon()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        app = create_app()
    except (SecretError, StoreError, ConfigError) as exc:
        logging.getLogger("countdown").critical("refusing to start: %s", exc)
        raise SystemExit(1) from None
    # Single worker is mandatory: the scheduler and queue live in this process.
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        log_level="info",
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),  # trust only the reverse proxy
    )


if __name__ == "__main__":
    run()
