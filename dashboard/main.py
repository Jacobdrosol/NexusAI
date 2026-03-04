import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="NexusAI Dashboard", version="0.1.0")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/dashboard", response_class=RedirectResponse)
    async def dashboard_root():
        return RedirectResponse(url="/dashboard/workers")

    @app.get("/dashboard/workers", response_class=HTMLResponse)
    async def dashboard_workers(request: Request):
        workers = []
        error = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{CONTROL_PLANE_URL}/v1/workers")
                resp.raise_for_status()
                workers = resp.json()
        except Exception as e:
            error = str(e)
            logger.warning("Failed to fetch workers: %s", e)
        return templates.TemplateResponse(
            "workers.html",
            {"request": request, "workers": workers, "error": error},
        )

    @app.get("/dashboard/bots", response_class=HTMLResponse)
    async def dashboard_bots(request: Request):
        bots = []
        error = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{CONTROL_PLANE_URL}/v1/bots")
                resp.raise_for_status()
                bots = resp.json()
        except Exception as e:
            error = str(e)
            logger.warning("Failed to fetch bots: %s", e)
        return templates.TemplateResponse(
            "bots.html",
            {"request": request, "bots": bots, "error": error},
        )

    @app.get("/dashboard/tasks", response_class=HTMLResponse)
    async def dashboard_tasks(request: Request):
        tasks = []
        error = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{CONTROL_PLANE_URL}/v1/tasks")
                resp.raise_for_status()
                tasks = resp.json()
        except Exception as e:
            error = str(e)
            logger.warning("Failed to fetch tasks: %s", e)
        return templates.TemplateResponse(
            "tasks.html",
            {"request": request, "tasks": tasks, "error": error},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "dashboard.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("DASHBOARD_PORT", "8080")),
        reload=False,
    )
