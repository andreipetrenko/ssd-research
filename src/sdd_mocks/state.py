"""In-memory model of the Bitbucket DC and Jira DC behaviour that the orchestrator relies on."""

import hashlib
import hmac
import itertools
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class MockError(Exception):
    def __init__(self, status_code: int, message: str, exception_name: str = "com.atlassian.mock.Error"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.exception_name = exception_name


@dataclass
class Delivery:
    id: int
    event_key: str
    url: str
    headers: dict[str, str]
    body: bytes
    pull_request: str
    created_at: str = field(default_factory=now_iso)
    status_code: int | None = None
    response: str = ""
    error: str | None = None


@dataclass
class Repo:
    project: str
    slug: str
    commits: dict[str, dict] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    prs: dict[int, dict] = field(default_factory=dict)
    comments: dict[int, list[dict]] = field(default_factory=dict)
    next_pr: int = 1

    def to_json(self) -> dict:
        return {"slug": self.slug, "name": self.slug, "project": {"key": self.project}}


DEFAULT_IDEAS = [
    {
        "summary": "Онлайн-заявка на кредитные каникулы",
        "description": (
            "Клиенты звонят в контакт-центр, чтобы оформить кредитные каникулы; среднее ожидание 14 минут. "
            "Нужна заявка в мобильном банке с проверкой права на каникулы и статусом рассмотрения."
        ),
    },
    {"summary": "Уведомления о просрочке через push", "description": ""},
]

LINK_TYPES = {
    "Relates": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
    "Blocks": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
}

TRANSITIONS = [
    {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
    {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Done", "to": {"name": "Done"}},
]


class MockState:
    def __init__(self, public_url: str, webhook_url: str, webhook_secret: str = "",
                 seed_files: dict[tuple[str, str], dict[str, str]] | None = None):
        self.public_url = public_url.rstrip("/")
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.seed_files = seed_files or {}
        self.lock = threading.RLock()
        self.dispatcher: Callable[[Delivery], None] = self._dispatch_in_thread
        self.reset()

    # --- lifecycle --------------------------------------------------------------------------------

    def reset(self) -> None:
        with self.lock:
            self.sequence = itertools.count(1)
            self.repos: dict[tuple[str, str], Repo] = {}
            self.webhooks: dict[str, list[dict]] = {}
            self.builds: dict[str, list[dict]] = {}
            self.deliveries: list[Delivery] = []
            self.issues: dict[str, dict] = {}
            self.counters = {"IDEA": 123, "ANL": 57, "DEV": 231}
            self.projects = {
                "IDEA": {"name": "Бизнес-идеи", "types": ["Business Idea"]},
                "ANL": {"name": "Системный анализ", "types": ["Task", "Story"]},
                "DEV": {"name": "Разработка", "types": ["Task", "Story", "Bug"]},
            }
            self.faults = {"mode": "off", "remaining": 0}
            self.fault_log: list[str] = []
            for project, slug in (("SPEC", "business-specs"), ("APP", "application-repo")):
                repo = Repo(project, slug)
                files = self.seed_files.get((project, slug)) or {"README.md": f"# {slug}\n"}
                sha = self._commit(repo, files, "Initial commit", parent=None)
                repo.branches["main"] = sha
                self.repos[(project, slug)] = repo
            for project in ("SPEC", "APP"):
                self.webhooks[project] = [{
                    "id": next(self.sequence), "name": "SDD Orchestrator", "url": self.webhook_url,
                    "active": True,
                    "events": ["pr:opened", "pr:from_ref_updated", "pr:merged", "pr:declined",
                               "pr:reviewer:needs_work", "pr:reviewer:approved"],
                    "configuration": {"secret": self.webhook_secret} if self.webhook_secret else {},
                }]
            for idea in DEFAULT_IDEAS:
                self.create_issue({
                    "project": {"key": "IDEA"}, "issuetype": {"name": "Business Idea"}, **idea,
                    "labels": ["business-idea"],
                }, bypass_faults=True)

    # --- Bitbucket --------------------------------------------------------------------------------

    def repo(self, project: str, slug: str) -> Repo:
        repo = self.repos.get((project, slug))
        if repo is None:
            raise MockError(404, f"Repository {project}/{slug} does not exist.",
                            "com.atlassian.bitbucket.repository.NoSuchRepositoryException")
        return repo

    def _commit(self, repo: Repo, files: dict[str, str], message: str, parent: str | None) -> str:
        seed = f"{repo.project}/{repo.slug}:{parent}:{message}:{json.dumps(files, sort_keys=True)}:{uuid.uuid4()}"
        sha = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        repo.commits[sha] = {"files": dict(files), "message": message, "parent": parent, "ts": now_ms()}
        return sha

    def push(self, project: str, slug: str, branch: str, files: dict[str, str], message: str,
             base: str = "main", delete: list[str] | None = None) -> str:
        with self.lock:
            repo = self.repo(project, slug)
            parent = repo.branches.get(branch) or repo.branches[base]
            snapshot = dict(repo.commits[parent]["files"])
            snapshot.update(files)
            for path in delete or []:
                snapshot.pop(path, None)
            sha = self._commit(repo, snapshot, message, parent)
            repo.branches[branch] = sha
            pending = []
            for pr in repo.prs.values():
                if pr["state"] == "OPEN" and pr["fromRef"]["displayId"] == branch:
                    pr["fromRef"]["latestCommit"] = sha
                    pr["version"] += 1
                    pr["updatedDate"] = now_ms()
                    for reviewer in pr["reviewers"]:
                        reviewer.update(approved=False, status="UNAPPROVED")
                    pending.append(self._prepare_delivery(repo, pr, "pr:from_ref_updated"))
        self._send(pending)
        return sha

    def file_at(self, project: str, slug: str, path: str, at: str | None) -> str:
        with self.lock:
            repo = self.repo(project, slug)
            ref = (at or "main").removeprefix("refs/heads/")
            sha = repo.branches.get(ref, ref)
            commit = repo.commits.get(sha)
            if commit is None:
                raise MockError(404, f"Commit {at} does not exist.")
            if path not in commit["files"]:
                raise MockError(404, f"The path \"{path}\" does not exist at revision \"{at}\"",
                                "com.atlassian.bitbucket.content.NoSuchPathException")
            return commit["files"][path]

    def _pr_url(self, repo: Repo, pr_id: int) -> str:
        return f"{self.public_url}/projects/{repo.project}/repos/{repo.slug}/pull-requests/{pr_id}"

    def create_pr(self, project: str, slug: str, body: dict) -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            from_branch = body["fromRef"]["id"].removeprefix("refs/heads/")
            to_branch = body["toRef"]["id"].removeprefix("refs/heads/")
            if from_branch not in repo.branches:
                raise MockError(404, f"Repository {project}/{slug} has no branch {from_branch}.")
            if to_branch not in repo.branches:
                raise MockError(404, f"Repository {project}/{slug} has no branch {to_branch}.")
            for pr in repo.prs.values():
                if pr["state"] == "OPEN" and pr["fromRef"]["displayId"] == from_branch \
                        and pr["toRef"]["displayId"] == to_branch:
                    raise MockError(409, f"Only one pull request may be open for a given source and target "
                                         f"branch (existing #{pr['id']}).",
                                    "com.atlassian.bitbucket.pull.DuplicatePullRequestException")
            pr_id = repo.next_pr
            repo.next_pr += 1
            reviewers = [r["user"]["name"] for r in body.get("reviewers", [])] or ["reviewer"]
            pr = {
                "id": pr_id, "version": 0, "title": body.get("title", from_branch),
                "description": body.get("description", ""), "state": "OPEN", "open": True, "closed": False,
                "createdDate": now_ms(), "updatedDate": now_ms(),
                "fromRef": {"id": f"refs/heads/{from_branch}", "displayId": from_branch,
                            "latestCommit": repo.branches[from_branch], "repository": repo.to_json()},
                "toRef": {"id": f"refs/heads/{to_branch}", "displayId": to_branch,
                          "latestCommit": repo.branches[to_branch], "repository": repo.to_json()},
                "author": {"user": {"name": body.get("author", "analyst"), "displayName": "Аналитик"},
                           "role": "AUTHOR", "approved": False, "status": "UNAPPROVED"},
                "reviewers": [{"user": {"name": name, "displayName": name}, "role": "REVIEWER",
                               "approved": False, "status": "UNAPPROVED"} for name in reviewers],
                "participants": [],
                "properties": {},
                "links": {"self": [{"href": self._pr_url(repo, pr_id)}]},
            }
            repo.prs[pr_id] = pr
            repo.comments[pr_id] = []
            pending = [self._prepare_delivery(repo, pr, "pr:opened")]
        self._send(pending)
        return pr

    def get_pr(self, project: str, slug: str, pr_id: int) -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            if pr_id not in repo.prs:
                raise MockError(404, f"Pull request {pr_id} does not exist in {project}/{slug}.",
                                "com.atlassian.bitbucket.pull.NoSuchPullRequestException")
            return repo.prs[pr_id]

    def list_prs(self, project: str, slug: str, state: str = "OPEN", at: str | None = None) -> list[dict]:
        with self.lock:
            repo = self.repo(project, slug)
            result = []
            for pr in repo.prs.values():
                if state != "ALL" and pr["state"] != state:
                    continue
                if at and pr["fromRef"]["id"] != at and pr["fromRef"]["displayId"] != at:
                    continue
                result.append(pr)
            return sorted(result, key=lambda p: -p["id"])

    def set_review(self, project: str, slug: str, pr_id: int, user: str, status: str) -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            pr = self.get_pr(project, slug, pr_id)
            if pr["state"] != "OPEN":
                raise MockError(409, "Pull request is not open.")
            reviewer = next((r for r in pr["reviewers"] if r["user"]["name"] == user), None)
            if reviewer is None:
                reviewer = {"user": {"name": user, "displayName": user}, "role": "REVIEWER"}
                pr["reviewers"].append(reviewer)
            reviewer.update(status=status, approved=status == "APPROVED")
            event = {"APPROVED": "pr:reviewer:approved", "NEEDS_WORK": "pr:reviewer:needs_work"}.get(status)
            pending = [self._prepare_delivery(repo, pr, event)] if event else []
        self._send(pending)
        return reviewer

    def merge_pr(self, project: str, slug: str, pr_id: int) -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            pr = self.get_pr(project, slug, pr_id)
            if pr["state"] != "OPEN":
                raise MockError(409, f"Pull request is already {pr['state']}.",
                                "com.atlassian.bitbucket.pull.IllegalPullRequestStateException")
            source_sha = repo.branches[pr["fromRef"]["displayId"]]
            target = pr["toRef"]["displayId"]
            files = repo.commits[source_sha]["files"]
            merge_sha = self._commit(
                repo, files, f"Merge pull request #{pr_id} from {pr['fromRef']['displayId']}",
                parent=repo.branches[target],
            )
            repo.branches[target] = merge_sha
            pr.update(state="MERGED", open=False, closed=True, closedDate=now_ms(), updatedDate=now_ms())
            pr["version"] += 1
            pr["toRef"]["latestCommit"] = merge_sha
            pr["properties"]["mergeCommit"] = {"id": merge_sha, "displayId": merge_sha[:11]}
            pending = [self._prepare_delivery(repo, pr, "pr:merged")]
        self._send(pending)
        return pr

    def decline_pr(self, project: str, slug: str, pr_id: int) -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            pr = self.get_pr(project, slug, pr_id)
            if pr["state"] != "OPEN":
                raise MockError(409, f"Pull request is already {pr['state']}.")
            pr.update(state="DECLINED", open=False, closed=True, closedDate=now_ms(), updatedDate=now_ms())
            pr["version"] += 1
            pending = [self._prepare_delivery(repo, pr, "pr:declined")]
        self._send(pending)
        return pr

    def add_comment(self, project: str, slug: str, pr_id: int, text: str, author: str = "sdd-bot") -> dict:
        with self.lock:
            repo = self.repo(project, slug)
            self.get_pr(project, slug, pr_id)
            comment = {"id": next(self.sequence), "text": text, "author": {"name": author},
                       "createdDate": now_ms()}
            repo.comments[pr_id].append(comment)
            return comment

    def set_build(self, commit: str, state: str, key: str = "ci", name: str = "CI pipeline") -> dict:
        with self.lock:
            builds = [b for b in self.builds.get(commit, []) if b["key"] != key]
            build = {"state": state, "key": key, "name": name, "url": f"{self.public_url}/#ci-{commit[:8]}",
                     "dateAdded": now_ms()}
            builds.append(build)
            self.builds[commit] = builds
            return build

    def redeliver(self, project: str, slug: str, pr_id: int) -> Delivery:
        with self.lock:
            label = f"{project}/{slug}#{pr_id}"
            original = next((d for d in reversed(self.deliveries) if d.pull_request == label), None)
            if original is None:
                raise MockError(404, f"No webhook was delivered for {label}")
            copy = Delivery(id=len(self.deliveries) + 1, event_key=original.event_key, url=original.url,
                            headers=dict(original.headers), body=original.body, pull_request=label)
            self.deliveries.append(copy)
        self._send([copy])
        return copy

    def _prepare_delivery(self, repo: Repo, pr: dict, event_key: str) -> list[Delivery]:
        hooks = [h for h in self.webhooks.get(repo.project, []) if h["active"] and event_key in h["events"]]
        deliveries = []
        for hook in hooks:
            body = json.dumps({
                "eventKey": event_key, "date": now_iso(),
                "actor": {"name": "mock-user", "displayName": "Mock user"},
                "pullRequest": pr,
            }, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8", "X-Event-Key": event_key,
                       "X-Request-Id": str(uuid.uuid4())}
            secret = hook.get("configuration", {}).get("secret")
            if secret:
                digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
                headers["X-Hub-Signature"] = f"sha256={digest}"
            delivery = Delivery(id=len(self.deliveries) + 1, event_key=event_key, url=hook["url"],
                                headers=headers, body=body, pull_request=f"{repo.project}/{repo.slug}#{pr['id']}")
            self.deliveries.append(delivery)
            deliveries.append(delivery)
        return deliveries

    def _send(self, batches: list) -> None:
        for item in batches:
            for delivery in item if isinstance(item, list) else [item]:
                self.dispatcher(delivery)

    def _dispatch_in_thread(self, delivery: Delivery) -> None:
        threading.Thread(target=self.deliver_now, args=(delivery,), daemon=True).start()

    def deliver_now(self, delivery: Delivery, client: httpx.Client | None = None) -> None:
        try:
            if client is not None:
                response = client.post(delivery.url, content=delivery.body, headers=delivery.headers)
            else:
                response = httpx.post(delivery.url, content=delivery.body, headers=delivery.headers, timeout=30)
            delivery.status_code = response.status_code
            delivery.response = response.text[:2000]
        except httpx.HTTPError as exc:
            delivery.error = f"{exc.__class__.__name__}: {exc}"

    # --- Jira -------------------------------------------------------------------------------------

    def set_faults(self, mode: str, count: int) -> dict:
        with self.lock:
            self.faults = {"mode": mode, "remaining": count if mode != "off" else 0}
            return dict(self.faults)

    def jira_gate(self, operation: str) -> None:
        """Simulate Jira outages: 'down' fails any call, 'fail_after_create' fails after creating an issue."""
        with self.lock:
            if self.faults["mode"] == "down" and self.faults["remaining"] != 0:
                self.faults["remaining"] -= 1
                self.fault_log.append(f"{now_iso()} 503 на {operation}")
                if self.faults["remaining"] == 0:
                    self.faults["mode"] = "off"
                raise MockError(503, "Jira is temporarily unavailable (simulated outage)")

    def create_issue(self, fields: dict, bypass_faults: bool = False) -> dict:
        if not bypass_faults:
            self.jira_gate("POST /issue")
        with self.lock:
            project = fields.get("project", {}).get("key")
            if project not in self.projects:
                raise MockError(400, f"project: Проект {project!r} не найден")
            issue_type = fields.get("issuetype", {}).get("name")
            if issue_type not in self.projects[project]["types"]:
                raise MockError(400, f"issuetype: Тип {issue_type!r} недоступен в проекте {project}")
            if not fields.get("summary"):
                raise MockError(400, "summary: Поле обязательно")
            self.counters[project] = self.counters.get(project, 0)
            number = self.counters[project]
            self.counters[project] += 1
            key = f"{project}-{number}"
            issue = {
                "id": str(10000 + len(self.issues)), "key": key, "self": f"{self.public_url}/rest/api/2/issue/{key}",
                "fields": {
                    **{k: v for k, v in fields.items() if k.startswith("customfield_") or k == "components"},
                    "project": {"key": project, "name": self.projects[project]["name"]},
                    "issuetype": {"name": issue_type},
                    "summary": fields["summary"],
                    "description": fields.get("description", ""),
                    "labels": list(fields.get("labels", [])),
                    "status": {"name": "To Do"},
                    "issuelinks": [],
                    "created": now_iso(),
                    "updated": now_iso(),
                    "comment": {"comments": [], "total": 0},
                },
                "remotelinks": [],
            }
            self.issues[key] = issue
            fail_after = self.faults["mode"] == "fail_after_create" and self.faults["remaining"] != 0
            if fail_after:
                self.faults["remaining"] -= 1
                if self.faults["remaining"] == 0:
                    self.faults["mode"] = "off"
                self.fault_log.append(f"{now_iso()} {key} создана, но клиент получил 500")
        if fail_after:
            raise MockError(500, "Connection reset after the issue was created (simulated)")
        return issue

    def issue(self, key: str) -> dict:
        self.jira_gate(f"GET /issue/{key}")
        with self.lock:
            if key not in self.issues:
                raise MockError(404, "Issue Does Not Exist")
            return self.issues[key]

    def search(self, jql: str) -> list[dict]:
        import re

        self.jira_gate("GET /search")
        labels = re.findall(r'labels\s*=\s*"?([^"\s]+)"?', jql)
        projects = re.findall(r'project\s*=\s*"?(\w+)"?', jql)
        with self.lock:
            result = []
            for issue in self.issues.values():
                fields = issue["fields"]
                if any(label not in fields["labels"] for label in labels):
                    continue
                if projects and fields["project"]["key"] not in projects:
                    continue
                result.append(issue)
            return result

    def link(self, link_type: str, inward: str, outward: str) -> None:
        self.jira_gate("POST /issueLink")
        with self.lock:
            if link_type not in LINK_TYPES:
                raise MockError(404, f"No issue link type with name '{link_type}' found.")
            for key in (inward, outward):
                if key not in self.issues:
                    raise MockError(404, f"Issue {key} Does Not Exist")
            kind = LINK_TYPES[link_type]
            link_id = str(next(self.sequence))
            self.issues[inward]["fields"]["issuelinks"].append(
                {"id": link_id, "type": kind, "outwardIssue": {"key": outward}})
            self.issues[outward]["fields"]["issuelinks"].append(
                {"id": link_id, "type": kind, "inwardIssue": {"key": inward}})

    def comment(self, key: str, body: str) -> dict:
        issue = self.issue(key)
        with self.lock:
            comment = {"id": str(next(self.sequence)), "body": body, "author": {"name": "sdd-bot"},
                       "created": now_iso()}
            issue["fields"]["comment"]["comments"].append(comment)
            issue["fields"]["comment"]["total"] += 1
            return comment

    def remote_link(self, key: str, body: dict) -> dict:
        issue = self.issue(key)
        with self.lock:
            global_id = body.get("globalId")
            for link in issue["remotelinks"]:
                if global_id and link.get("globalId") == global_id:
                    link["object"] = body["object"]
                    return link
            link = {"id": next(self.sequence), "globalId": global_id, "object": body["object"]}
            issue["remotelinks"].append(link)
            return link

    def transition(self, key: str, transition_id: str) -> None:
        issue = self.issue(key)
        with self.lock:
            match = next((t for t in TRANSITIONS if t["id"] == transition_id), None)
            if match is None:
                raise MockError(400, f"Transition id '{transition_id}' is not valid for this issue.")
            issue["fields"]["status"] = {"name": match["to"]["name"]}
            issue["fields"]["updated"] = now_iso()
