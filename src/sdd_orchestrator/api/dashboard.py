from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..domain import STAGE_TITLES, Stage
from ..services.http import IntegrationError
from ..services.workflow_service import IdeaError
from .admin import _services, build_trace, health

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

PIPELINE = [Stage.IDEA, Stage.BUSINESS_SPECIFICATION, Stage.SYSTEM_ANALYSIS,
            Stage.FUNCTIONAL_SPECIFICATION, Stage.DEVELOPMENT, Stage.DONE]

STATUS_TITLES = {
    "registered": "зарегистрировано", "in_progress": "в работе", "in_review": "на ревью",
    "changes_requested": "на доработке", "declined": "отклонено", "done": "завершено",
    "received": "получено", "processing": "обрабатывается", "failed": "ошибка, повтор",
    "dead": "исчерпаны попытки", "rejected": "отклонено", "ignored": "проигнорировано",
}

EVENT_TITLES = {
    "pull_request_opened": "PR открыт", "pull_request_updated": "PR обновлён",
    "pull_request_merged": "PR слит", "pull_request_declined": "PR отклонён",
    "pull_request_needs_work": "PR на доработку", "pull_request_approved": "PR одобрен",
}


def _redirect(message: str, kind: str = "ok") -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"msg": message, "kind": kind}), status_code=303)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} мс"
    if seconds < 120:
        return f"{seconds:.1f} с"
    if seconds < 7200:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds / 3600:.1f} ч"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str | None = None, kind: str = "ok", partial: bool = False):
    services = _services(request)
    changes = [build_trace(services, change) for change in services.changes.recent()]
    events = services.events.recent(limit=60)
    metrics = services.events.metrics()
    by_status = metrics["events_by_status"]
    context = {
        "changes": changes,
        "events": events,
        "metrics": metrics,
        "processed": by_status.get("done", 0),
        "pending_retry": by_status.get("failed", 0) + by_status.get("processing", 0),
        "problems": by_status.get("dead", 0) + by_status.get("rejected", 0),
        "health": health(request),
        "settings": services.settings,
        "pipeline": PIPELINE,
        "stage_titles": STAGE_TITLES,
        "stage_index": {stage.value: i for i, stage in enumerate(PIPELINE)},
        "status_titles": STATUS_TITLES,
        "event_titles": EVENT_TITLES,
        "duration": _duration,
        "msg": msg,
        "kind": kind,
    }
    name = "_live.html" if partial else "dashboard.html"
    return templates.TemplateResponse(request, name, context)


@router.post("/ui/ideas")
def ui_register_idea(request: Request, source_issue: str = Form(...)):
    key = source_issue.strip().upper()
    try:
        change, created = _services(request).workflow.register_idea(key)
    except IdeaError as exc:
        details = "; ".join(exc.problems)
        return _redirect(f"{exc}{': ' + details if details else ''}", "error")
    except IntegrationError as exc:
        return _redirect(f"Jira недоступна: {exc}", "error")
    verb = "Зарегистрировано изменение" if created else "Изменение уже существует:"
    return _redirect(f"{verb} {change['change_id']} для {key}. Ветка: feature/{change['change_id']}-business-spec")


@router.post("/ui/events/{event_id}/retry")
def ui_retry(event_id: int, request: Request):
    result = _services(request).workflow.process(event_id, force=True)
    status = result.get("status")
    text = {"done": "обработано", "failed": "снова ошибка", "dead": "попытки исчерпаны",
            "rejected": "отклонено"}.get(status, status)
    detail = result.get("error") or result.get("reason") or ""
    return _redirect(f"Событие #{event_id}: {text}. {detail}".strip(), "ok" if status == "done" else "error")


@router.post("/ui/recheck")
def ui_recheck(request: Request, project_key: str = Form(...), repository_slug: str = Form(...),
               pull_request_id: int = Form(...)):
    try:
        result = _services(request).workflow.recheck_pull_request(
            project_key.strip(), repository_slug.strip(), pull_request_id
        )
    except IntegrationError as exc:
        return _redirect(f"Bitbucket недоступен: {exc}", "error")
    status = result.get("status")
    detail = result.get("reason") or result.get("error") or ""
    label = {"duplicate": "уже обработан ранее, дубликат не создан", "done": "обработан"}.get(status, status)
    return _redirect(f"PR {project_key}/{repository_slug}#{pull_request_id}: {label}. {detail}".strip(),
                     "error" if status in ("failed", "dead", "rejected") else "ok")
