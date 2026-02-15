# app/main.py
import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.utils.config import get_settings
from app.utils.logging import setup_logging
from app.utils.files import ensure_dir
from app.api.routes import router as api_router
import app.api.routes as routes_module
from app.services.jobs import JobStore

log = logging.getLogger("app")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    ensure_dir(settings.tmp_dir)

    routes_module.JOB_STORE = JobStore(tmp_root=settings.tmp_dir)

    app = FastAPI(title="QAD DeepAgents", version="0.1.0")
    app.include_router(api_router)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_dir = os.path.join(repo_root, "web")
    static_dir = os.path.join(web_dir, "static")

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    def _startup_cleanup():
        try:
            from app.services.cleanup import CleanupConfig, cleanup_old_jobs
            summary = cleanup_old_jobs(CleanupConfig(tmp_dir=settings.tmp_dir, retention_days=settings.job_retention_days))
            log.info("Job cleanup summary: %s", summary)
        except Exception as e:
            log.warning("Job cleanup failed: %s", e)

    @app.get("/", include_in_schema=False)
    def home():
        index_path = os.path.join(web_dir, "index.html")
        return FileResponse(index_path)

    @app.get("/ui-config", include_in_schema=False)
    def ui_config():
        return {
            "allow_debug_downloads": bool(settings.ui_allow_debug_downloads),
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # Convenience alias (common expectation)
    @app.get("/health")
    def health():
        return {"status": "ok"}

    log.info("FastAPI app created (env=%s, tmp_dir=%s)", settings.app_env, settings.tmp_dir)
    return app


app = create_app()
