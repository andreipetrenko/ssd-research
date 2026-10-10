"""End-to-end scenarios from sections 10 and 13: mock Bitbucket/Jira → webhook → PostgreSQL → Jira."""

import hashlib
import hmac
import json

from conftest import example
from sqlalchemy import text

from sdd_orchestrator.workers.retry_worker import RetryWorker


def _events(harness, **filters):
    return harness.services.events.recent(**filters)


def test_business_idea_must_have_required_fields(harness):
    response = harness.orchestrator.post("/api/changes", json={"source_issue": "IDEA-124"})
    assert response.status_code == 422
    assert "description" in json.dumps(response.json(), ensure_ascii=False)
    assert harness.orchestrator.post("/api/changes", json={"source_issue": "IDEA-999"}).status_code == 404


def test_registering_the_same_idea_twice_returns_the_same_change(harness):
    first = harness.orchestrator.post("/api/changes", json={"source_issue": "IDEA-123"}).json()
    second = harness.orchestrator.post("/api/changes", json={"source_issue": "IDEA-123"}).json()
    assert first["change_id"] == second["change_id"] == "CHG-1042"
    assert first["created"] is True and second["created"] is False


def test_full_flow_from_idea_to_merged_code(harness):
    change_id, business_pr = harness.business_spec_merged()

    analysis = harness.issues("ANL")
    assert len(analysis) == 1
    fields = analysis[0]["fields"]
    assert fields["summary"].startswith(f"[SDD] Системный анализ: {change_id}")
    assert f"Change ID: {change_id}" in fields["description"]
    assert {"sdd", change_id} <= set(fields["labels"])
    assert "IDEA-123" in {link.get("outwardIssue", {}).get("key") for link in fields["issuelinks"]}
    assert any("метаданные и документ проверены" in c for c in harness.comments(business_pr))
    assert any(analysis[0]["key"] in c for c in harness.comments(business_pr))

    harness.functional_spec_merged(change_id)
    dev = sorted(harness.issues("DEV"), key=lambda i: i["key"])
    assert [i["fields"]["summary"].split(": ", 1)[0] for i in dev] == [
        f"[SDD] {change_id} · DEV-1", f"[SDD] {change_id} · DEV-2", f"[SDD] {change_id} · DEV-3",
    ]
    blocks = [link for link in dev[0]["fields"]["issuelinks"] if link["type"]["name"] == "Blocks"]
    assert {link["outwardIssue"]["key"] for link in blocks} == {dev[1]["key"], dev[2]["key"]}
    trace = harness.orchestrator.get(f"/api/changes/{change_id}").json()
    assert trace["stage"] == "development"

    for issue in dev:
        key = issue["key"]
        branch = f"feature/{change_id}-{key}-implementation"
        harness.push(branch, {f"src/{key}.py": "print('ok')\n"}, project="APP", repo="application-repo")
        pr = harness.open_pr(branch, f"Change ID: {change_id}\nStage: implementation\nJira: {key}",
                             project="APP", repo="application-repo")
        harness.flush()
        assert harness.state.issues[key]["remotelinks"], "PR должен быть связан с Jira-задачей"
        harness.action(pr, "approve", project="APP", repo="application-repo")
        harness.action(pr, "build", project="APP", repo="application-repo", state="SUCCESSFUL")
        harness.action(pr, "merge", project="APP", repo="application-repo")
        result = harness.flush_event("pr:merged")
        assert result["status"] == "done", result
        assert harness.state.issues[key]["fields"]["status"]["name"] == "Done"

    trace = harness.orchestrator.get(f"/api/changes/{change_id}").json()
    assert (trace["stage"], trace["status"]) == ("done", "done")
    kinds = {a["artifact_type"] for a in trace["artifacts"]}
    assert kinds == {"business_specification", "functional_specification", "implementation"}
    assert all(a["merge_commit_sha"] for a in trace["artifacts"])


def test_redelivered_webhook_does_not_create_duplicates(harness):
    change_id, pr = harness.business_spec_merged()
    for _ in range(3):
        harness.action(pr, "redeliver")
        body = harness.flush()[0]["body"]
        assert body["status"] == "duplicate"
    assert len(harness.issues("ANL")) == 1
    merged = _events(harness, change_id=change_id)
    merge_event = next(e for e in merged if e["event_type"] == "pull_request_merged")
    assert merge_event["duplicate_count"] == 3
    assert harness.orchestrator.get("/api/metrics").json()["duplicates_prevented"] >= 3


def test_manual_recheck_recovers_a_lost_webhook_without_duplicates(harness):
    change_id, pr = harness.business_spec_merged()
    harness.state.dispatcher = lambda delivery: None  # webhooks are lost from now on
    body = {"project_key": "SPEC", "repository_slug": "business-specs", "pull_request_id": pr["id"]}
    assert harness.orchestrator.post("/api/pull-requests/recheck", json=body).json()["status"] == "duplicate"
    assert len(harness.issues("ANL")) == 1

    with harness.services.engine.begin() as conn:
        conn.execute(text("DELETE FROM sdd_event WHERE event_type = 'pull_request_merged'"))
    result = harness.orchestrator.post("/api/pull-requests/recheck", json=body).json()
    assert result["status"] == "done"
    assert result["created_issues"] == []
    assert len(harness.issues("ANL")) == 1


def test_jira_outage_is_retried_by_the_worker(harness):
    change_id = harness.register_idea()
    branch = f"feature/{change_id}-business-spec"
    path = harness.settings.business_spec_path.format(change_id=change_id)
    harness.push(branch, {path: example("business-spec.md", CHANGE_ID=change_id, IDEA_KEY="IDEA-123")})
    pr = harness.open_pr(branch, f"Change ID: {change_id}\nJira: IDEA-123")
    harness.flush()
    harness.action(pr, "approve")
    harness.mock.post("/mock/jira/faults", json={"mode": "down", "count": 1})
    harness.action(pr, "merge")
    body = harness.flush_event("pr:merged")
    assert body["status"] == "failed"
    assert harness.issues("ANL") == []

    worker = RetryWorker(harness.services.workflow, harness.services.events, harness.settings)
    statuses = [r["status"] for r in worker.run_once()]
    assert statuses == ["done"]
    assert len(harness.issues("ANL")) == 1
    event = next(e for e in _events(harness) if e["event_type"] == "pull_request_merged")
    assert event["status"] == "done" and event["attempts"] == 2


def test_crash_after_jira_created_the_issue_is_reconciled(harness):
    harness.mock.post("/mock/jira/faults", json={"mode": "fail_after_create", "count": 1})
    change_id, _ = harness.business_spec_merged()
    assert len(harness.issues("ANL")) == 1, "задача создана в Jira, но ответ потерян"
    event = next(e for e in _events(harness) if e["event_type"] == "pull_request_merged")
    assert event["status"] == "failed"

    result = harness.orchestrator.post(f"/api/events/{event['id']}/retry").json()
    assert result["status"] == "done"
    assert result["created_issues"] == []
    assert result["reconciled_issues"] == [harness.issues("ANL")[0]["key"]]
    assert len(harness.issues("ANL")) == 1


def test_merge_without_approval_is_rejected(harness):
    change_id = harness.register_idea()
    branch = f"feature/{change_id}-business-spec"
    path = harness.settings.business_spec_path.format(change_id=change_id)
    harness.push(branch, {path: example("business-spec.md", CHANGE_ID=change_id, IDEA_KEY="IDEA-123")})
    pr = harness.open_pr(branch, f"Change ID: {change_id}")
    harness.flush()
    harness.action(pr, "merge")
    body = harness.flush_event("pr:merged")
    assert body["status"] == "rejected"
    assert "одобрений" in body["reason"]
    assert harness.issues("ANL") == []


def test_invalid_spec_gets_review_comment_and_blocks_transition(harness):
    change_id = harness.register_idea()
    branch = f"feature/{change_id}-business-spec"
    path = harness.settings.business_spec_path.format(change_id=change_id)
    broken = example("business-spec.md", CHANGE_ID=change_id, IDEA_KEY="IDEA-123").replace("## 7. Бизнес-правила", "")
    harness.push(branch, {path: broken})
    pr = harness.open_pr(branch, f"Change ID: {change_id}")
    harness.flush()
    assert any("не прошёл проверку" in c and "Бизнес-правила" in c for c in harness.comments(pr))

    harness.action(pr, "needs-work")
    harness.flush()
    assert harness.orchestrator.get(f"/api/changes/{change_id}").json()["status"] == "changes_requested"

    good = example("business-spec.md", CHANGE_ID=change_id, IDEA_KEY="IDEA-123")
    harness.push(branch, {path: good})
    harness.flush()
    assert harness.orchestrator.get(f"/api/changes/{change_id}").json()["status"] == "in_review"
    assert "PR готов к ревью" in harness.comments(pr)[-1]


def test_code_pr_waits_for_ci(harness):
    change_id, _ = harness.business_spec_merged()
    harness.functional_spec_merged(change_id)
    key = sorted(harness.issues("DEV"), key=lambda i: i["key"])[0]["key"]
    branch = f"feature/{change_id}-{key}-implementation"
    harness.push(branch, {"src/app.py": "x = 1\n"}, project="APP", repo="application-repo")
    pr = harness.open_pr(branch, "", project="APP", repo="application-repo")
    harness.flush()
    harness.action(pr, "approve", project="APP", repo="application-repo")
    harness.action(pr, "merge", project="APP", repo="application-repo")
    body = harness.flush_event("pr:merged")
    assert body["status"] == "failed" and "CI" in body["error"]
    harness.state.set_build(pr["fromRef"]["latestCommit"], "SUCCESSFUL")
    assert RetryWorker(harness.services.workflow, harness.services.events, harness.settings).run_once()[0][
        "status"] == "done"
    assert harness.state.issues[key]["fields"]["status"]["name"] == "Done"


def test_pr_from_unknown_repository_or_without_change_id_is_not_processed(harness):
    harness.push("feature/no-convention", {"notes.md": "x"})
    pr = harness.open_pr("feature/no-convention", "просто PR")
    body = harness.flush()[0]["body"]
    assert body["status"] == "rejected"
    assert any("PR не принят" in c for c in harness.comments(pr))


def test_webhook_signature_is_verified(harness, settings):
    settings.bitbucket_webhook_secret = "s3cret"
    payload = json.dumps({"eventKey": "diagnostics:ping"}).encode()
    bad = harness.orchestrator.post("/webhooks/bitbucket", content=payload,
                                    headers={"X-Event-Key": "diagnostics:ping", "X-Hub-Signature": "sha256=00"})
    assert bad.status_code == 401
    signature = hmac.new(b"s3cret", payload, hashlib.sha256).hexdigest()
    good = harness.orchestrator.post("/webhooks/bitbucket", content=payload,
                                     headers={"X-Event-Key": "diagnostics:ping",
                                              "X-Hub-Signature": f"sha256={signature}"})
    assert good.json() == {"status": "pong"}


def test_dashboard_and_metrics_render(harness):
    assert "Пока нет ни одного изменения" in harness.orchestrator.get("/").text
    change_id, _ = harness.business_spec_merged()
    page = harness.orchestrator.get("/").text
    assert change_id in page and "Системный анализ" in page
    assert "sdd_webhook_deliveries_total" in harness.orchestrator.get("/metrics").text
    assert harness.orchestrator.get("/health").json()["database"] == "ok"
