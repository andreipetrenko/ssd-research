from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..container import Services
from ..domain import ARTIFACT_TITLES, STAGE_TITLES, ArtifactType, Stage, branch_name
from ..services.http import IntegrationError
from ..services.workflow_service import IdeaError

router = APIRouter()


def _services(request: Request) -> Services:
    return request.app.state.services


class IdeaRequest(BaseModel):
    source_issue: str = Field(pattern=r"^[A-Z][A-Z0-9]+-\d+$", examples=["IDEA-123"])


class RecheckRequest(BaseModel):
    project_key: str
    repository_slug: str
    pull_request_id: int


def build_trace(services: Services, change: dict) -> dict:
    settings, jira = services.settings, services.jira
    change_id = change["change_id"]
    artifacts = services.changes.artifacts(change_id)
    issues = services.changes.issues(change_id)
    for artifact in artifacts:
        artifact["title"] = ARTIFACT_TITLES[ArtifactType(artifact["artifact_type"])]
    for issue in issues:
        issue["url"] = jira.browse_url(settings.jira_browse_base, issue["issue_key"])
    return {
        **change,
        "stage_title": STAGE_TITLES[Stage(change["stage"])],
        "source_issue_url": jira.browse_url(settings.jira_browse_base, change["source_issue"]),
        "business_branch": branch_name(change_id, ArtifactType.BUSINESS_SPECIFICATION),
        "system_branch": branch_name(change_id, ArtifactType.FUNCTIONAL_SPECIFICATION),
        "business_spec_path": settings.business_spec_path.format(change_id=change_id),
        "functional_spec_path": settings.functional_spec_path.format(change_id=change_id),
        "artifacts": artifacts,
        "issues": issues,
    }


@router.get("/health")
def health(request: Request) -> dict:
    services = _services(request)
    try:
        with services.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db = "ok"
    except Exception as exc:  # noqa: BLE001
        db = f"error: {exc.__class__.__name__}"
    worker = request.app.state.worker
    return {"status": "ok" if db == "ok" else "degraded", "database": db,
            "retry_worker": bool(worker and worker._thread and worker._thread.is_alive())}


@router.get("/api/metrics")
def metrics_json(request: Request) -> dict:
    return _services(request).events.metrics()


@router.get("/metrics", response_class=PlainTextResponse)
def metrics_prometheus(request: Request) -> str:
    data = _services(request).events.metrics()
    lines = [
        "# HELP sdd_webhook_deliveries_total Webhook deliveries received (including duplicates).",
        "# TYPE sdd_webhook_deliveries_total counter",
        f"sdd_webhook_deliveries_total {data['deliveries']}",
        "# HELP sdd_events Events by processing status.",
        "# TYPE sdd_events gauge",
    ]
    for status, count in sorted(data["events_by_status"].items()):
        lines.append(f'sdd_events{{status="{status}"}} {count}')
    lines += [
        "# TYPE sdd_duplicates_prevented_total counter",
        f"sdd_duplicates_prevented_total {data['duplicates_prevented']}",
        "# TYPE sdd_retries_total counter",
        f"sdd_retries_total {data['retries']}",
    ]
    for name in ("merge_to_jira_avg_seconds", "merge_to_jira_max_seconds", "idea_to_code_pr_avg_seconds"):
        if data[name] is not None:
            lines += [f"# TYPE sdd_{name} gauge", f"sdd_{name} {data[name]:.3f}"]
    return "\n".join(lines) + "\n"


@router.get("/api/changes")
def list_changes(request: Request) -> list[dict]:
    services = _services(request)
    return [build_trace(services, change) for change in services.changes.recent()]


@router.post("/api/changes", status_code=201)
def register_idea(body: IdeaRequest, request: Request) -> dict:
    services = _services(request)
    try:
        change, created = services.workflow.register_idea(body.source_issue)
    except IdeaError as exc:
        raise HTTPException(exc.status_code, {"message": str(exc), "problems": exc.problems}) from exc
    except IntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"created": created, **build_trace(services, change)}


@router.get("/api/changes/{change_id}")
def get_change(change_id: str, request: Request) -> dict:
    services = _services(request)
    change = services.changes.get(change_id)
    if change is None:
        raise HTTPException(404, f"change {change_id} not found")
    return {**build_trace(services, change), "events": services.events.recent(change_id=change_id)}


@router.get("/api/events")
def list_events(request: Request, status: str | None = None, change_id: str | None = None,
                limit: int = 100) -> list[dict]:
    return _services(request).events.recent(status=status, change_id=change_id, limit=min(limit, 500))


@router.post("/api/events/{event_id}/retry")
def retry_event(event_id: int, request: Request) -> dict:
    services = _services(request)
    if services.events.get(event_id) is None:
        raise HTTPException(404, f"event {event_id} not found")
    return services.workflow.process(event_id, force=True)


@router.post("/api/pull-requests/recheck")
def recheck(body: RecheckRequest, request: Request) -> dict:
    try:
        return _services(request).workflow.recheck_pull_request(
            body.project_key, body.repository_slug, body.pull_request_id
        )
    except IntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
