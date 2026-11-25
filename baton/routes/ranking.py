"""Model ranking leaderboard and human review routes (/ranking)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ranking_view
from ..ranking import CRITERIA, historical_score
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/ranking", response_class=HTMLResponse)
def ranking_page(request: Request, origin: Literal["all", "delegation", "task"] = "all",
                 view: Literal["agent", "human", "combined"] = "agent", saved: int = 0,
                 tab: Literal["all", "pending"] = "all", model: str | None = None,
                 real: int = 0, page_number: int = Query(1, alias="page"),
                 ctx: AppContext = CTX):
    models, jobs = ctx.ranking.overview(origin)
    if real:
        models, jobs = ranking_view.without_estimates(models, jobs)
    rows = ranking_view.leaderboard(models, jobs, view)
    if model is not None and model not in {r["model"] for r in rows}:
        model = None
    listed = ranking_view.job_list(jobs, tab, model)
    return ctx.page(
        request, "ranking.html", "ranking", models=models, rows=rows, origin=origin,
        view=view, saved=bool(saved), tab=tab, model=model, real=bool(real),
        selected_label=ranking_view.model_label(model or ""),
        stats=ranking_view.summary(rows, jobs, view),
        pending_count=len(ranking_view.job_list(jobs, "pending", model)),
        jobs=ranking_view.paginate(listed, page_number, ranking_view.PER_PAGE_JOBS),
    )


@router.get("/ranking/{job}", response_class=HTMLResponse)
def delegation_page(request: Request, job: int,
                    origin: Literal["all", "delegation", "task"] = "all",
                    view: Literal["agent", "human", "combined"] = "agent",
                    ctx: AppContext = CTX):
    details, reviews = ctx.ranking.detail(job)
    if details is None:
        raise HTTPException(404, "Unknown delegation")
    return ctx.page(
        request, "delegation.html", "ranking", job=details, reviews=reviews,
        criteria=CRITERIA, estimate=historical_score(details), origin=origin, view=view,
    )


@router.post("/ranking/{job}")
def human_review(request: Request, job: int, csrf: str = Form(""),
                 fulfillment: int = Form(..., ge=1, le=10),
                 correctness: int = Form(..., ge=1, le=10), rework: int = Form(..., ge=1, le=10),
                 comment: str = Form("", max_length=500),
                 origin: Literal["all", "delegation", "task"] = Form("all"),
                 view: Literal["agent", "human", "combined"] = Form("agent"),
                 back: str = Form(""), ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    try:
        ctx.ranking.review(job, "human", fulfillment, correctness, rework, comment)
    except KeyError as exc:
        raise HTTPException(404, "Unknown delegation") from exc
    # Back to the list (same filter, view and page), with a one-time confirmation.
    if back.startswith("/ranking?") or back == "/ranking":
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}saved=1#jobs", status_code=303)
    return RedirectResponse(f"/ranking?origin={origin}&view={view}&saved=1", status_code=303)
