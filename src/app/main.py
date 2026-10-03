"""FastAPI entrypoint providing an HTMX + Jinja2 interface.

Run with:
    uvicorn src.app.main:app --reload
"""
from __future__ import annotations
from src.presentation.api.routers.auth_proxy import router as auth_proxy_router

from pathlib import Path
from datetime import datetime
import os
import logging
from contextlib import asynccontextmanager

# Third-party imports
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import SQLModel
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

# Local imports
from src.infrastructure.db import models  # noqa: F401
from src.infrastructure.db.engine import engine, wait_for_database
from src.presentation.api.routers import api_router as api
from src.presentation.web.views import views
from src.core.auth import extract_token_from_request, verify_supabase_token, ensure_uuid


class HealthCheckFilter(logging.Filter):
    """Filter out healthcheck endpoint requests from access logs."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for healthcheck requests to exclude them from logs."""
        return record.getMessage().find("/healthz") == -1


# Configure logging to filter healthcheck requests
if os.getenv("LOG_FILTER_HEALTHCHECK", "").lower() in ("true", "1", "yes"):
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

BASE_DIR = Path(__file__).resolve().parent
# Repo root is two levels up from src/app
ROOT_DIR = BASE_DIR.parent.parent
TEMPLATES_DIR = BASE_DIR.parent / "templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Load environment variables from .env at repo root (safe in dev/local)
try:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
except Exception:
    # Proceed without .env if loading fails
    pass

app_name = os.getenv("APP_NAME", "Marks Manager")
app_version = os.getenv("APP_VERSION", "dev")
raw_api_version = os.getenv("API_VERSION", "v1").strip().strip("/")
api_version = f"v{raw_api_version}" if raw_api_version.isdigit() else raw_api_version

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Application lifespan context.

    Startup: create tables (idempotent) and initialize Jinja2 environment.
    Shutdown: currently no actions (placeholder for future resource cleanup).
    """
    # Startup
    # Ensure the database is reachable before creating tables (handles container races)
    wait_for_database()
    # For development: drop and recreate tables on each startup
    # SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    # Column migration: add gpa_scale to courses table for existing databases.
    # create_all is idempotent but does not add columns to existing tables.
    from sqlalchemy import inspect as _sa_inspect, text as _text
    from sqlmodel import Session as _Session, select as _select
    from src.infrastructure.db.models import GradeScale as _GradeScale

    _inspector = _sa_inspect(engine)
    _course_cols = {c["name"] for c in _inspector.get_columns("courses")}
    if "gpa_scale" not in _course_cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE courses ADD COLUMN gpa_scale INTEGER DEFAULT 4"))
            conn.commit()

    _subject_cols = {c["name"] for c in _inspector.get_columns("subjects")}
    if "is_finalized" not in _subject_cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE subjects ADD COLUMN is_finalized BOOLEAN NOT NULL DEFAULT FALSE"))
            conn.commit()

    # Seed 7-Point Australian GradeScale rows once if they don't exist yet.
    with _Session(engine) as _s:
        _exists = _s.exec(
            _select(_GradeScale).where(_GradeScale.scale_name == "7-Point Australian")
        ).first()
        if not _exists:
            _seven_pt = [
                _GradeScale(scale_name="7-Point Australian", grade="HD", label="High Distinction", min_mark=85.0, gpa_point=7.0, band_type="both"),
                _GradeScale(scale_name="7-Point Australian", grade="D",  label="Distinction",       min_mark=75.0, gpa_point=6.0, band_type="both"),
                _GradeScale(scale_name="7-Point Australian", grade="C",  label="Credit",            min_mark=65.0, gpa_point=5.0, band_type="both"),
                _GradeScale(scale_name="7-Point Australian", grade="P",  label="Pass",              min_mark=50.01, gpa_point=4.0, band_type="both"),
                _GradeScale(scale_name="7-Point Australian", grade="PS", label="Pass Supplementary", min_mark=50.0, gpa_point=4.0, band_type="both"),
                _GradeScale(scale_name="7-Point Australian", grade="F",  label="Fail",              min_mark=0.0,  gpa_point=0.0, band_type="both"),
            ]
            for _r in _seven_pt:
                _s.add(_r)
            _s.commit()
    # Enable template auto-reload in development so Jinja picks up template changes without restarts
    fastapi_app.state.jinja_env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        auto_reload=True,
        cache_size=0,  # avoid template caching during active development
    )
    # Provide global template variables
    fastapi_app.state.jinja_env.globals.update(
        current_year=str(datetime.now().year),
        app_version=app_version,
        env_name=os.getenv("ENV", "dev"),
        api_version=api_version,
        app_name=app_name,
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", ""),
    )
    # ...existing code...
    yield
    # Shutdown (no-op)


APPLICATION = FastAPI(title="University Marks Manager API", lifespan=lifespan)

# Use API_VERSION for API prefix
API_PREFIX = f"/api/{api_version}" if api_version else "/api"
APPLICATION.include_router(api, prefix=API_PREFIX)
if raw_api_version and raw_api_version != api_version:
    APPLICATION.include_router(api, prefix=f"/api/{raw_api_version}")
APPLICATION.include_router(views)
APPLICATION.include_router(auth_proxy_router)


# Supabase Auth Middleware to validate Bearer tokens and set request.state.user
@APPLICATION.middleware("http")
async def supabase_auth_middleware(request: Request, call_next):
    token = extract_token_from_request(request)
    if token:
        try:
            payload = verify_supabase_token(token)
            request.state.user = payload
            request.state.user_id = ensure_uuid(payload["sub"])
        except Exception:
            pass
    return await call_next(request)

# Enable server-side sessions for lightweight state (e.g., selected course)
# NOTE: Replace the secret key with an environment variable for production use.
session_secret = os.getenv("SESSION_SECRET_KEY")
if not session_secret:
    raise RuntimeError("SESSION_SECRET_KEY environment variable is required for SessionMiddleware")
APPLICATION.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    same_site="lax",
)

# Optionally serve static (tailwind compiled CSS could go here later)
static_dir = BASE_DIR.parent / "static"
static_dir.mkdir(exist_ok=True)
APPLICATION.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Mount original assets (icons) if present at repo root / assets
assets_dir = ROOT_DIR / "assets"
if assets_dir.exists():
    APPLICATION.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

# Explicit /favicon.ico for browsers requesting root path
static_favicon = static_dir / "images" / "favicon.ico"
assets_favicon = assets_dir / "favicon.ico"
if static_favicon.exists() or assets_favicon.exists():
    @APPLICATION.get("/favicon.ico")
    def favicon():
        """
        Short description.

        Raises:
            Description.
        """
        path = str(static_favicon if static_favicon.exists() else assets_favicon)
        return FileResponse(path)
app = APPLICATION  # backwards compatible name for uvicorn target

__all__ = ["app", "APPLICATION"]

# --- No-Cache Middleware ---
@APPLICATION.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Root-level health endpoint (does not depend on API router mounting)
@APPLICATION.get("/healthz")
def healthz():
    """
    Short description.

    Raises:
        Description.
    """
    return {"status": "ok"}

@APPLICATION.get("/manifest.json", include_in_schema=False)
async def serve_manifest():
    return FileResponse(static_dir / "manifest.json")

@APPLICATION.get("/sw.js", include_in_schema=False)
async def serve_sw():
    return FileResponse(static_dir / "sw.js")

