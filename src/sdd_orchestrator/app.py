import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import admin, dashboard, webhook_handler
from .container import Services, build_services
from .db import run_migrations
from .settings import Settings, get_settings
from .workers.retry_worker import RetryWorker


def create_app(settings: Settings | None = None, services: Services | None = None,
               start_worker: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    services = services or build_services(settings)
    run_worker = settings.run_retry_worker if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_migrate:
            run_migrations(services.engine, settings.migrations_dir)
        if app.state.worker:
            app.state.worker.start()
        yield
        if app.state.worker:
            app.state.worker.stop()

    app = FastAPI(
        title="SDD Orchestrator",
        description="Integration Service: события Bitbucket PR → переходы процесса → задачи Jira.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.worker = RetryWorker(services.workflow, services.events, settings) if run_worker else None
    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
    app.include_router(webhook_handler.router, tags=["webhooks"])
    app.include_router(admin.router, tags=["api"])
    app.include_router(dashboard.router, include_in_schema=False)
    return app
