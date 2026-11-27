"""FastAPI route modules for Baton."""

from __future__ import annotations

from fastapi import FastAPI

from . import api, auth, chat, machines, models, projects, quotas, ranking, tasks, townhall
from .deps import AppContext

ROUTERS = (
    auth.router,
    projects.router,
    quotas.router,
    ranking.router,
    tasks.router,
    models.router,
    machines.router,
    chat.router,
    townhall.router,
    api.router,
)


def register_routes(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)


__all__ = ["AppContext", "ROUTERS", "register_routes"]
