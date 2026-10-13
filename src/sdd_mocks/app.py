"""Mock Bitbucket Data Center + Jira Data Center for local runs, demos and end-to-end tests."""

import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .state import LINK_TYPES, TRANSITIONS, MockError, MockState

ROOT = Path(__file__).resolve().parents[2]
BB = "/rest/api/latest/projects/{project}/repos/{slug}"
JIRA = "/rest/api/2"


def _seed_files() -> dict[tuple[str, str], dict[str, str]]:
    seeds: dict[tuple[str, str], dict[str, str]] = {}
    for project, slug in (("SPEC", "business-specs"), ("APP", "application-repo")):
        base = ROOT / "openspec-templates" / slug
        if base.is_dir():
            seeds[(project, slug)] = {
                str(path.relative_to(base)): path.read_text(encoding="utf-8")
                for path in sorted(base.rglob("*")) if path.is_file()
            }
    return seeds


def create_mock_app(state: MockState | None = None) -> FastAPI:
    state = state or MockState(
        public_url=os.environ.get("SDD_MOCK_PUBLIC_URL", "http://127.0.0.1:48090"),
        webhook_url=os.environ.get("SDD_MOCK_WEBHOOK_URL", "http://127.0.0.1:48080/webhooks/bitbucket"),
        webhook_secret=os.environ.get("SDD_BITBUCKET_WEBHOOK_SECRET", ""),
        seed_files=_seed_files(),
    )
    app = FastAPI(title="Mock Bitbucket DC + Jira DC", version="0.1.0")
    app.state.mock = state
    templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

    @app.exception_handler(MockError)
    def _mock_error(request: Request, exc: MockError):
        if request.url.path.startswith(JIRA):
            return JSONResponse({"errorMessages": [exc.message], "errors": {}}, status_code=exc.status_code)
        return JSONResponse({"errors": [{"message": exc.message, "exceptionName": exc.exception_name}]},
                            status_code=exc.status_code)

    # --- Bitbucket REST ---------------------------------------------------------------------------

    @app.get(BB + "/pull-requests")
    def list_prs(project: str, slug: str, request: Request, at: str | None = None):
        values = state.list_prs(project, slug, request.query_params.get("state", "OPEN"), at)
        return {"size": len(values), "limit": 100, "isLastPage": True, "start": 0, "values": values}

    @app.post(BB + "/pull-requests", status_code=201)
    def create_pr(project: str, slug: str, body: dict = Body(...)):
        return state.create_pr(project, slug, body)

    @app.get(BB + "/pull-requests/{pr_id}")
    def get_pr(project: str, slug: str, pr_id: int):
        return state.get_pr(project, slug, pr_id)

    @app.post(BB + "/pull-requests/{pr_id}/merge")
    def merge_pr(project: str, slug: str, pr_id: int):
        return state.merge_pr(project, slug, pr_id)

    @app.post(BB + "/pull-requests/{pr_id}/decline")
    def decline_pr(project: str, slug: str, pr_id: int):
        return state.decline_pr(project, slug, pr_id)

    @app.put(BB + "/pull-requests/{pr_id}/participants/{user}")
    def review_pr(project: str, slug: str, pr_id: int, user: str, body: dict = Body(...)):
        return state.set_review(project, slug, pr_id, user, body.get("status", "APPROVED"))

    @app.post(BB + "/pull-requests/{pr_id}/comments", status_code=201)
    def comment_pr(project: str, slug: str, pr_id: int, body: dict = Body(...)):
        return state.add_comment(project, slug, pr_id, body.get("text", ""))

    @app.get(BB + "/raw/{path:path}", response_class=PlainTextResponse)
    def raw(project: str, slug: str, path: str, at: str | None = None):
        return state.file_at(project, slug, path, at)

    @app.get(BB + "")
    def get_repo(project: str, slug: str):
        return state.repo(project, slug).to_json()

    @app.get("/rest/api/latest/projects/{project}/webhooks")
    def list_webhooks(project: str):
        values = state.webhooks.get(project, [])
        return {"size": len(values), "isLastPage": True, "values": values}

    @app.post("/rest/api/latest/projects/{project}/webhooks", status_code=201)
    def create_webhook(project: str, body: dict = Body(...)):
        hook = {"id": next(state.sequence), "active": True, **body}
        state.webhooks.setdefault(project, []).append(hook)
        return hook

    @app.get(BB + "/webhooks")
    def list_repo_webhooks(project: str, slug: str):
        state.repo(project, slug)
        values = state.webhooks.get(f"{project}/{slug}", [])
        return {"size": len(values), "isLastPage": True, "values": values}

    @app.post(BB + "/webhooks", status_code=201)
    def create_repo_webhook(project: str, slug: str, body: dict = Body(...)):
        state.repo(project, slug)
        hook = {"id": next(state.sequence), "active": True, **body}
        state.webhooks.setdefault(f"{project}/{slug}", []).append(hook)
        return hook

    @app.get("/rest/build-status/1.0/commits/{commit}")
    def builds(commit: str):
        values = state.builds.get(commit, [])
        return {"size": len(values), "isLastPage": True, "values": values}

    @app.post("/rest/build-status/1.0/commits/{commit}", status_code=204)
    def post_build(commit: str, body: dict = Body(...)):
        state.set_build(commit, body["state"], body.get("key", "ci"), body.get("name", "CI"))

    # --- Jira REST --------------------------------------------------------------------------------

    @app.post(JIRA + "/issue", status_code=201)
    def create_issue(body: dict = Body(...)):
        issue = state.create_issue(body.get("fields", {}))
        return {"id": issue["id"], "key": issue["key"], "self": issue["self"]}

    @app.get(JIRA + "/issue/createmeta/{project}/issuetypes")
    def createmeta(project: str):
        if project not in state.projects:
            raise MockError(404, f"Project {project} not found")
        types = state.projects[project]["types"]
        return {"values": [{"id": str(i + 1), "name": name, "subtask": False} for i, name in enumerate(types)]}

    @app.get(JIRA + "/issue/createmeta")
    def createmeta_fields(projectKeys: str = "", issuetypeNames: str = ""):  # noqa: N803
        project = projectKeys
        if project not in state.projects:
            return {"projects": []}
        types = [{"id": str(i + 1), "name": name, "fields": {
            "summary": {"required": True, "name": "Summary"},
            "description": {"required": name == "Business Idea", "name": "Description"},
            "issuetype": {"required": True, "name": "Issue Type"},
            "project": {"required": True, "name": "Project"},
        }} for i, name in enumerate(state.projects[project]["types"]) if not issuetypeNames or name == issuetypeNames]
        return {"projects": [{"key": project, "issuetypes": types}]}

    @app.get(JIRA + "/issue/{key}")
    def get_issue(key: str):
        issue = state.issue(key)
        return {k: v for k, v in issue.items() if k != "remotelinks"}

    @app.put(JIRA + "/issue/{key}", status_code=204)
    def update_issue(key: str, body: dict = Body(...)):
        issue = state.issue(key)
        issue["fields"].update(body.get("fields", {}))

    @app.get(JIRA + "/search")
    def search(jql: str = "", maxResults: int = 50):  # noqa: N803 - Jira parameter name
        issues = state.search(jql)[:maxResults]
        return {"startAt": 0, "maxResults": maxResults, "total": len(issues),
                "issues": [{k: v for k, v in i.items() if k != "remotelinks"} for i in issues]}

    @app.post(JIRA + "/issueLink", status_code=201)
    def issue_link(body: dict = Body(...)):
        state.link(body["type"]["name"], body["inwardIssue"]["key"], body["outwardIssue"]["key"])

    @app.get(JIRA + "/issueLinkType")
    def link_types():
        return {"issueLinkTypes": list(LINK_TYPES.values())}

    @app.post(JIRA + "/issue/{key}/comment", status_code=201)
    def add_comment(key: str, body: dict = Body(...)):
        return state.comment(key, body.get("body", ""))

    @app.get(JIRA + "/issue/{key}/remotelink")
    def remote_links(key: str):
        return state.issue(key)["remotelinks"]

    @app.post(JIRA + "/issue/{key}/remotelink", status_code=201)
    def add_remote_link(key: str, body: dict = Body(...)):
        return state.remote_link(key, body)

    @app.get(JIRA + "/issue/{key}/transitions")
    def transitions(key: str):
        state.issue(key)
        return {"transitions": TRANSITIONS}

    @app.post(JIRA + "/issue/{key}/transitions", status_code=204)
    def do_transition(key: str, body: dict = Body(...)):
        state.transition(key, body["transition"]["id"])

    # --- mock control API (used by the demo script and tests) -------------------------------------

    @app.post("/mock/bitbucket/push")
    def push(body: dict = Body(...)):
        sha = state.push(body["project"], body["repo"], body["branch"], body.get("files", {}),
                         body.get("message", "Update"), body.get("base", "main"), body.get("delete"))
        return {"commit": sha}

    @app.post("/mock/bitbucket/{project}/{slug}/pull-requests/{pr_id}/{action}")
    def pr_action(project: str, slug: str, pr_id: int, action: str, body: dict = Body(default={})):
        return _pr_action(project, slug, pr_id, action, body.get("user", "reviewer"), body.get("state"))

    @app.post("/mock/jira/faults")
    def faults(body: dict = Body(...)):
        return state.set_faults(body.get("mode", "off"), int(body.get("count", 1)))

    @app.post("/mock/reset")
    def reset():
        state.reset()
        return {"status": "reset"}

    @app.get("/mock/state")
    def dump():
        with state.lock:
            return {
                "issues": state.issues,
                "pull_requests": {f"{p}/{s}": repo.prs for (p, s), repo in state.repos.items()},
                "comments": {f"{p}/{s}": repo.comments for (p, s), repo in state.repos.items()},
                "deliveries": [{"id": d.id, "event": d.event_key, "pr": d.pull_request, "status": d.status_code,
                                "error": d.error} for d in state.deliveries],
                "faults": state.faults,
            }

    def _pr_action(project: str, slug: str, pr_id: int, action: str, user: str, build_state: str | None):
        if action == "approve":
            return state.set_review(project, slug, pr_id, user, "APPROVED")
        if action == "needs-work":
            return state.set_review(project, slug, pr_id, user, "NEEDS_WORK")
        if action == "merge":
            return state.merge_pr(project, slug, pr_id)
        if action == "decline":
            return state.decline_pr(project, slug, pr_id)
        if action == "redeliver":
            delivery = state.redeliver(project, slug, pr_id)
            return {"delivery": delivery.id, "event": delivery.event_key}
        if action == "build":
            pr = state.get_pr(project, slug, pr_id)
            return state.set_build(pr["fromRef"]["latestCommit"], build_state or "SUCCESSFUL")
        raise MockError(404, f"unknown action {action}")

    # --- UI ---------------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str | None = None, kind: str = "ok"):
        with state.lock:
            repos = []
            for (project, slug), repo in state.repos.items():
                prs = []
                for pr in sorted(repo.prs.values(), key=lambda p: -p["id"]):
                    builds = state.builds.get(pr["fromRef"]["latestCommit"], [])
                    prs.append({"pr": pr, "comments": repo.comments.get(pr["id"], []),
                                "build": builds[-1]["state"] if builds else None})
                repos.append({"project": project, "slug": slug, "prs": prs,
                              "branches": sorted(repo.branches.items())})
            issues = sorted(state.issues.values(),
                            key=lambda i: (i["fields"]["project"]["key"], int(i["key"].split("-")[1])))
            deliveries = list(reversed(state.deliveries[-30:]))
        return templates.TemplateResponse(request, "mock.html", {
            "repos": repos, "issues": issues, "deliveries": deliveries, "faults": state.faults,
            "fault_log": list(reversed(state.fault_log[-10:])), "webhook_url": state.webhook_url,
            "msg": msg, "kind": kind,
        })

    def _back(msg: str, kind: str = "ok", anchor: str = "") -> RedirectResponse:
        return RedirectResponse("/?" + urlencode({"msg": msg, "kind": kind}) + anchor, status_code=303)

    @app.post("/ui/pr/{project}/{slug}/{pr_id}/{action}")
    def ui_pr_action(project: str, slug: str, pr_id: int, action: str, user: str = Form("reviewer"),
                     build_state: str = Form("SUCCESSFUL")):
        try:
            _pr_action(project, slug, pr_id, action, user, build_state)
        except MockError as exc:
            return _back(exc.message, "error", f"#pr-{project}-{slug}-{pr_id}")
        labels = {"approve": "одобрен", "needs-work": "возвращён на доработку", "merge": "слит",
                  "decline": "отклонён", "redeliver": "webhook отправлен повторно",
                  "build": f"статус CI: {build_state}"}
        return _back(f"PR {project}/{slug}#{pr_id}: {labels.get(action, action)}", "ok",
                     f"#pr-{project}-{slug}-{pr_id}")

    @app.post("/ui/faults")
    def ui_faults(mode: str = Form(...), count: int = Form(1)):
        state.set_faults(mode, count)
        text = {"off": "Сбои Jira выключены", "down": f"Jira будет недоступна для {count} запросов",
                "fail_after_create": f"Jira будет обрывать ответ после создания задачи ({count} раз)"}
        return _back(text.get(mode, mode))

    @app.post("/ui/ideas")
    def ui_idea(summary: str = Form(...), description: str = Form("")):
        try:
            issue = state.create_issue({"project": {"key": "IDEA"}, "issuetype": {"name": "Business Idea"},
                                        "summary": summary, "description": description,
                                        "labels": ["business-idea"]}, bypass_faults=True)
        except MockError as exc:
            return _back(exc.message, "error")
        return _back(f"Создана Business Idea {issue['key']}", "ok", f"#issue-{issue['key']}")

    @app.post("/ui/reset")
    def ui_reset():
        state.reset()
        return _back("Состояние моков сброшено")

    # --- browse links used in PR descriptions and Jira issues -------------------------------------

    @app.get("/browse/{key}")
    def browse_issue(key: str):
        return RedirectResponse(f"/#issue-{key}")

    @app.get("/projects/{project}/repos/{slug}/pull-requests/{pr_id}")
    def browse_pr(project: str, slug: str, pr_id: int):
        return RedirectResponse(f"/#pr-{project}-{slug}-{pr_id}")

    @app.get("/projects/{project}/repos/{slug}/browse/{path:path}", response_class=PlainTextResponse)
    def browse_file(project: str, slug: str, path: str, at: str | None = None):
        return state.file_at(project, slug, path, at)

    return app
