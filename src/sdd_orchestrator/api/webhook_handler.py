import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..domain import BITBUCKET_EVENT_MAP, EventType, NormalizedEvent, extract_pr_metadata
from ..services.bitbucket_client import merge_commit_of, pull_request_url

log = logging.getLogger(__name__)
router = APIRouter()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def normalize_pull_request(pr: dict, *, event_type: EventType, event_id: str, prefix: str,
                           occurred_at: str | None) -> NormalizedEvent:
    from_ref, to_ref = pr.get("fromRef", {}), pr.get("toRef", {})
    repository = to_ref.get("repository", {})
    source_branch = from_ref.get("displayId") or from_ref.get("id", "").removeprefix("refs/heads/")
    meta = extract_pr_metadata(prefix, pr.get("title", ""), pr.get("description", ""), source_branch)
    return NormalizedEvent(
        event_id=event_id,
        event_type=event_type,
        project_key=repository.get("project", {}).get("key", ""),
        repository_slug=repository.get("slug", ""),
        pull_request_id=int(pr["id"]),
        source_branch=source_branch,
        target_branch=to_ref.get("displayId") or to_ref.get("id", "").removeprefix("refs/heads/"),
        title=pr.get("title", ""),
        description=pr.get("description", ""),
        pull_request_url=pull_request_url(pr),
        latest_commit=from_ref.get("latestCommit"),
        merge_commit=merge_commit_of(pr) if event_type is EventType.PR_MERGED else None,
        change_id=meta.change_id,
        stage=meta.artifact_type.value if meta.artifact_type else None,
        jira_key=meta.jira_key,
        occurred_at=occurred_at,
        metadata_errors=meta.errors,
    )


def _occurred_at(payload: dict) -> str | None:
    date = payload.get("date")
    if date:
        try:
            return datetime.fromisoformat(date.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    closed = payload.get("pullRequest", {}).get("closedDate")
    if isinstance(closed, int | float):
        return datetime.fromtimestamp(closed / 1000, UTC).isoformat()
    return None


@router.post("/webhooks/bitbucket")
async def bitbucket_webhook(request: Request) -> dict:
    services = request.app.state.services
    settings = services.settings
    body = await request.body()
    if not verify_signature(settings.bitbucket_webhook_secret, body, request.headers.get("X-Hub-Signature")):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="payload is not JSON") from exc

    event_key = request.headers.get("X-Event-Key") or payload.get("eventKey", "")
    if event_key == "diagnostics:ping":
        return {"status": "pong"}
    event_type = BITBUCKET_EVENT_MAP.get(event_key)
    if event_type is None or "pullRequest" not in payload:
        return {"status": "ignored", "reason": f"event {event_key!r} is not used by SDD"}

    # Bitbucket does not always send a unique delivery id; assign one so the event can be traced.
    delivery_id = request.headers.get("X-Request-Id") or f"generated-{uuid.uuid4()}"
    event = normalize_pull_request(
        payload["pullRequest"], event_type=event_type, event_id=delivery_id,
        prefix=settings.change_id_prefix, occurred_at=_occurred_at(payload),
    )
    log.info("bitbucket %s %s#%s change=%s stage=%s", event_key, event.repository, event.pull_request_id,
             event.change_id, event.stage)
    return await run_in_threadpool(services.workflow.accept, event)
