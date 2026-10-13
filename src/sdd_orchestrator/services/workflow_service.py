import logging
from datetime import UTC, datetime

from ..domain import (
    ARTIFACT_TITLES,
    ISSUE_KEY_RE,
    ArtifactType,
    EventType,
    NormalizedEvent,
    Stage,
)
from ..repositories.changes import ChangeRepository
from ..repositories.events import EventRepository
from ..settings import Settings
from . import spec_parser
from .bitbucket_client import BitbucketClient, approvals_of, merge_commit_of, pull_request_url
from .http import IntegrationError
from .idempotency_service import IdempotencyService
from .jira_client import JiraClient

log = logging.getLogger(__name__)

STAGE_ORDER = [
    Stage.IDEA, Stage.BUSINESS_SPECIFICATION, Stage.SYSTEM_ANALYSIS,
    Stage.FUNCTIONAL_SPECIFICATION, Stage.DEVELOPMENT, Stage.DONE,
]


class Rejected(Exception):
    """The event is invalid and retrying will not help (wrong repo, invalid document, no approvals)."""


class NotReady(Exception):
    """The event is valid but a precondition is pending (e.g. CI still running); retried later."""


class IdeaError(Exception):
    def __init__(self, message: str, status_code: int = 422, problems: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.problems = problems or []


class WorkflowService:
    def __init__(self, settings: Settings, jira: JiraClient, bitbucket: BitbucketClient,
                 changes: ChangeRepository, events: EventRepository):
        self.settings = settings
        self.jira = jira
        self.bitbucket = bitbucket
        self.changes = changes
        self.events = events
        self.idempotency = IdempotencyService(jira, changes)

    # --- Business Idea registration ---------------------------------------------------------------

    def register_idea(self, source_issue: str) -> tuple[dict, bool]:
        issue = self.jira.get_issue(source_issue, fields="summary,description,issuetype,status")
        if issue is None:
            raise IdeaError(f"Задача {source_issue} не найдена в Jira", 404)
        fields = issue.get("fields", {})
        problems = []
        expected_type = self.settings.jira_idea_issue_type
        actual_type = (fields.get("issuetype") or {}).get("name")
        if expected_type and actual_type != expected_type:
            problems.append(f"Тип задачи «{actual_type}», ожидается «{expected_type}»")
        for name in self.settings.idea_required_field_list:
            if not str(fields.get(name) or "").strip():
                problems.append(f"Не заполнено обязательное поле {name}")
        if problems:
            raise IdeaError("Business Idea не готова к передаче бизнес-аналитику", 422, problems)
        return self.changes.allocate(self.settings.change_id_prefix, source_issue, fields.get("summary"))

    # --- event intake -----------------------------------------------------------------------------

    def operation_key(self, ev: NormalizedEvent) -> str:
        if ev.metadata_errors or not ev.change_id or not ev.stage:
            return f"bitbucket:{ev.repository}:{ev.pull_request_id}:{ev.event_type.value}:{ev.event_id}"
        chg, pr = ev.change_id, f"{ev.repository}:{ev.pull_request_id}"
        sha = ev.merge_commit or ev.latest_commit or "unknown"
        if ev.event_type is EventType.PR_MERGED:
            return {
                ArtifactType.BUSINESS_SPECIFICATION: f"{chg}:create-system-analysis:{sha}",
                ArtifactType.FUNCTIONAL_SPECIFICATION: f"{chg}:create-dev-tasks:{sha}",
                ArtifactType.IMPLEMENTATION: f"{chg}:complete-dev-task:{ev.jira_key}:{sha}",
            }[ArtifactType(ev.stage)]
        if ev.event_type in (EventType.PR_OPENED, EventType.PR_UPDATED):
            return f"{chg}:register-pr:{pr}:{ev.latest_commit}"
        if ev.event_type is EventType.PR_DECLINED:
            return f"{chg}:pr-declined:{pr}"
        return f"{chg}:{ev.event_type.value}:{pr}:{ev.event_id}"

    def _precheck(self, ev: NormalizedEvent) -> tuple[str, str | None]:
        """Return (status, reason) for events that must not be processed."""
        repo = ev.repository
        spec_repos, code_repos = self.settings.spec_repository_list, self.settings.code_repository_list
        if repo not in spec_repos and repo not in code_repos:
            return "ignored", f"Репозиторий {repo} не подключён к SDD"
        if ev.target_branch not in self.settings.target_branch_list:
            return "ignored", f"Целевая ветка {ev.target_branch} не отслеживается"
        if ev.metadata_errors:
            # Ordinary PRs in a shared repo must not get a bot comment.
            return "ignored", "; ".join(ev.metadata_errors)
        stage = ArtifactType(ev.stage)
        if stage is ArtifactType.IMPLEMENTATION and repo not in code_repos:
            return "rejected", f"PR с кодом пришёл из репозитория спецификаций {repo}"
        if stage is not ArtifactType.IMPLEMENTATION and repo not in spec_repos:
            return "rejected", f"PR спецификации пришёл из репозитория кода {repo}"
        return "received", None

    def accept(self, ev: NormalizedEvent) -> dict:
        if ev.event_type is EventType.PR_MERGED and not ev.merge_commit:
            try:
                pr = self.bitbucket.get_pull_request(ev.project_key, ev.repository_slug, ev.pull_request_id)
                ev.merge_commit = merge_commit_of(pr)
            except IntegrationError as exc:
                log.warning("cannot resolve merge commit for %s#%s: %s", ev.repository, ev.pull_request_id, exc)

        status, reason = self._precheck(ev)
        event, created = self.events.register(
            idempotency_key=self.operation_key(ev), event_type=ev.event_type.value, change_id=ev.change_id,
            payload=ev.to_dict(), delivery_id=ev.event_id, source_event_at=_parse_time(ev.occurred_at),
            status=status, last_error=reason,
        )
        if created and status == "rejected" and ev.event_type is EventType.PR_OPENED:
            self._comment_pr(ev, f"SDD: PR не принят в процесс. {reason}", [])
        if created and status != "received":
            return {"status": status, "event_id": event["id"], "reason": reason}
        if not created and event["status"] not in ("failed", "dead", "received"):
            self.events.record_duplicate(event["id"])
            return {"status": "duplicate", "event_id": event["id"], "original_status": event["status"],
                    "idempotency_key": event["idempotency_key"]}
        if not created:
            self.events.record_duplicate(event["id"])
        return self.process(event["id"], force=event["status"] == "dead")

    def process(self, event_id: int, *, force: bool = False) -> dict:
        claimed = self.events.claim(event_id, force=force, stale_seconds=self.settings.stale_processing_seconds)
        if claimed is None:
            current = self.events.get(event_id)
            return {"status": current["status"] if current else "missing", "event_id": event_id}
        ev = NormalizedEvent.from_dict(claimed["payload"])
        try:
            result = self.handle(ev, claimed["idempotency_key"])
        except Rejected as exc:
            self.events.mark_rejected(event_id, str(exc))
            log.info("event %s rejected: %s", event_id, exc)
            return {"status": "rejected", "event_id": event_id, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
            attempts = claimed["attempts"]
            dead = attempts >= self.settings.max_attempts
            delay = min(self.settings.retry_base_delay_seconds * 2 ** (attempts - 1),
                        self.settings.retry_max_delay_seconds)
            message = f"{exc.__class__.__name__}: {exc}"
            self.events.mark_failed(event_id, message, delay_seconds=delay, dead=dead)
            log.warning("event %s failed (attempt %s): %s", event_id, attempts, message)
            return {"status": "dead" if dead else "failed", "event_id": event_id, "error": message,
                    "attempts": attempts, "retry_in_seconds": None if dead else delay}
        self.events.mark_done(event_id, result, ev.change_id)
        return {"status": "done", "event_id": event_id, **result}

    # --- handlers ---------------------------------------------------------------------------------

    def handle(self, ev: NormalizedEvent, operation_key: str) -> dict:
        pr = self.bitbucket.get_pull_request(ev.project_key, ev.repository_slug, ev.pull_request_id)
        state = pr.get("state")
        artifact = ArtifactType(ev.stage)
        if ev.event_type is EventType.PR_MERGED:
            if state != "MERGED":
                raise Rejected(f"PR #{ev.pull_request_id} не слит: актуальное состояние {state}")
            target = pr.get("toRef", {}).get("displayId")
            if target not in self.settings.target_branch_list:
                raise Rejected(f"PR слит в неотслеживаемую ветку {target}")
            merge_sha = merge_commit_of(pr) or ev.merge_commit
            if not merge_sha:
                raise NotReady("Bitbucket не вернул merge commit")
            approvals = approvals_of(pr)
            if len(approvals) < self.settings.min_approvals:
                raise Rejected(
                    f"PR слит без необходимых одобрений: {len(approvals)} из {self.settings.min_approvals}"
                )
            handler = {
                ArtifactType.BUSINESS_SPECIFICATION: self._on_business_merged,
                ArtifactType.FUNCTIONAL_SPECIFICATION: self._on_functional_merged,
                ArtifactType.IMPLEMENTATION: self._on_implementation_merged,
            }[artifact]
            return handler(ev, pr, merge_sha, operation_key)
        if ev.event_type in (EventType.PR_OPENED, EventType.PR_UPDATED):
            if state != "OPEN":
                return {"skipped": f"PR уже в состоянии {state}"}
            return self._on_pr_opened(ev, pr, artifact)
        if ev.event_type is EventType.PR_DECLINED:
            self._save_artifact(ev, pr, artifact, state=state or "DECLINED")
            self._advance(ev.change_id, None, "declined")
            return {"pull_request_state": state}
        if ev.event_type is EventType.PR_NEEDS_WORK:
            self._save_artifact(ev, pr, artifact, state=state or "OPEN")
            self._advance(ev.change_id, None, "changes_requested")
            return {"changes_requested": True}
        self._save_artifact(ev, pr, artifact, state=state or "OPEN")
        return {"approvals": approvals_of(pr)}

    def _on_pr_opened(self, ev: NormalizedEvent, pr: dict, artifact: ArtifactType) -> dict:
        chg = ev.change_id
        commit = pr.get("fromRef", {}).get("latestCommit") or ev.latest_commit
        change = self.changes.get(chg)
        problems: list[str] = []
        lines = [f"Change ID: {chg}", f"Этап: {ARTIFACT_TITLES[artifact]}"]

        if artifact is ArtifactType.IMPLEMENTATION:
            if change is None:
                raise Rejected(f"Изменение {chg} не зарегистрировано")
            issue = self.changes.issue(ev.jira_key)
            if issue is None or issue["change_id"] != chg or issue["stage"] != Stage.DEVELOPMENT:
                raise Rejected(f"{ev.jira_key} не является задачей разработки изменения {chg}")
            url = pull_request_url(pr) or ev.pull_request_url
            if url:
                self.jira.add_remote_link(
                    ev.jira_key, url=url, title=f"PR #{ev.pull_request_id}: {pr.get('title', '')}",
                    global_id=f"sdd-pr-{ev.repository}-{ev.pull_request_id}",
                )
            self._save_artifact(ev, pr, artifact, state="OPEN", commit=commit)
            self._advance(chg, Stage.DEVELOPMENT, "in_review")
            lines.append(f"Jira: {self._issue_ref(ev.jira_key)}")
            self._comment_pr(ev, "SDD: PR связан с задачей разработки.", lines)
            return {"change_id": chg, "linked_issue": ev.jira_key}

        path = self._spec_path(artifact, chg)
        text = self.bitbucket.get_file(ev.project_key, ev.repository_slug, path, commit)
        spec = None
        business = None
        if artifact is ArtifactType.FUNCTIONAL_SPECIFICATION:
            if change is None:
                raise Rejected(f"Изменение {chg} не зарегистрировано")
            business = self.changes.merged_artifact(chg, ArtifactType.BUSINESS_SPECIFICATION)
            if business is None:
                problems.append("Бизнес-спецификация этого изменения ещё не согласована (нет merge)")
        if text is None:
            problems.append(f"Файл {path} не найден в ветке {ev.source_branch}")
        else:
            spec = spec_parser.validate(
                artifact, text, prefix=self.settings.change_id_prefix, change_id=chg,
                business_commit=business["merge_commit_sha"] if business else None,
            )
            problems += spec.errors

        if artifact is ArtifactType.BUSINESS_SPECIFICATION and change is None:
            source = ev.jira_key
            if not source and spec:
                found = ISSUE_KEY_RE.search(spec.metadata.get("исходная jira", ""))
                source = found.group(0) if found else None
            if not source:
                raise Rejected(f"Изменение {chg} не зарегистрировано и не указана исходная Business Idea")
            self.changes.ensure(chg, source, spec.title if spec else None,
                                Stage.BUSINESS_SPECIFICATION, "in_review")

        self._save_artifact(ev, pr, artifact, state="OPEN", commit=commit)
        stage = (Stage.BUSINESS_SPECIFICATION if artifact is ArtifactType.BUSINESS_SPECIFICATION
                 else Stage.FUNCTIONAL_SPECIFICATION)
        self._advance(chg, stage, "in_review", title=spec.title if spec and stage is Stage.BUSINESS_SPECIFICATION
                      else None)
        lines.append(f"Документ: {path} @ {commit[:12] if commit else '?'}")
        if problems:
            self._comment_pr(ev, "SDD: документ не прошёл проверку. Исправьте перед merge:",
                             lines + [f"- {p}" for p in problems])
        else:
            if spec and spec.tasks:
                lines.append(f"Задач в декомпозиции: {len(spec.tasks)}")
            self._comment_pr(ev, "SDD: метаданные и документ проверены, PR готов к ревью.", lines)
        return {"change_id": chg, "validation_errors": problems}

    def _on_business_merged(self, ev: NormalizedEvent, pr: dict, sha: str, operation_key: str) -> dict:
        chg = ev.change_id
        path = self._spec_path(ArtifactType.BUSINESS_SPECIFICATION, chg)
        text = self.bitbucket.get_file(ev.project_key, ev.repository_slug, path, sha)
        if text is None:
            raise Rejected(f"В merge commit {sha[:12]} нет файла {path}")
        spec = spec_parser.validate_business_spec(text, prefix=self.settings.change_id_prefix, change_id=chg)
        if spec.errors:
            raise Rejected("Бизнес-спецификация не прошла проверку: " + "; ".join(spec.errors))
        change = self.changes.get(chg)
        if change is None:
            found = ISSUE_KEY_RE.search(spec.metadata.get("исходная jira", ""))
            change = self.changes.ensure(chg, found.group(0), spec.title, Stage.BUSINESS_SPECIFICATION, "in_review")
        source_issue = change["source_issue"]

        self._save_artifact(ev, pr, ArtifactType.BUSINESS_SPECIFICATION, state="MERGED", merge_sha=sha)
        spec_url = self.bitbucket.browse_url(ev.project_key, ev.repository_slug, path, sha)
        title = _strip_prefix(spec.title)
        description = "\n".join([
            "Проанализировать согласованную бизнес-спецификацию.",
            f"Change ID: {chg}",
            f"Specification: {spec_url}",
            f"Specification commit: {sha}",
            f"Business Idea: {source_issue}",
            f"Pull request: {pull_request_url(pr) or ev.pull_request_url or '-'}",
        ])
        ensured = self.idempotency.ensure_issue(
            operation_key=operation_key, change_id=chg, stage=Stage.SYSTEM_ANALYSIS,
            project=self.settings.jira_analysis_project, issue_type=self.settings.jira_analysis_issue_type,
            summary=f"[SDD] Системный анализ: {chg} — {title}", description=description,
            parent_issue=source_issue, extra_fields=self.settings.extra_fields("analysis"),
        )
        link = self.idempotency.ensure_link(self.settings.jira_relates_link_type,
                                            inward=ensured.key, outward=source_issue)
        self._advance(chg, Stage.SYSTEM_ANALYSIS, "in_progress", title=spec.title)
        self._comment_pr(ev, "SDD: бизнес-спецификация согласована.", [
            f"Создана задача системного анализа: {self._issue_ref(ensured.key)}",
        ])
        return {
            "change_id": chg,
            "issues": [ensured.key],
            "created_issues": [ensured.key] if ensured.outcome == "created" else [],
            "reconciled_issues": [ensured.key] if ensured.outcome == "reconciled" else [],
            "links": {f"{ensured.key}->{source_issue}": link},
        }

    def _on_functional_merged(self, ev: NormalizedEvent, pr: dict, sha: str, operation_key: str) -> dict:
        chg = ev.change_id
        if self.changes.get(chg) is None:
            raise Rejected(f"Изменение {chg} не зарегистрировано")
        business = self.changes.merged_artifact(chg, ArtifactType.BUSINESS_SPECIFICATION)
        if business is None:
            raise Rejected(f"Бизнес-спецификация {chg} не согласована")
        path = self._spec_path(ArtifactType.FUNCTIONAL_SPECIFICATION, chg)
        text = self.bitbucket.get_file(ev.project_key, ev.repository_slug, path, sha)
        if text is None:
            raise Rejected(f"В merge commit {sha[:12]} нет файла {path}")
        spec = spec_parser.validate_functional_spec(
            text, prefix=self.settings.change_id_prefix, change_id=chg, business_commit=business["merge_commit_sha"]
        )
        if spec.errors:
            raise Rejected("Функциональная спецификация не прошла проверку: " + "; ".join(spec.errors))

        self._save_artifact(ev, pr, ArtifactType.FUNCTIONAL_SPECIFICATION, state="MERGED", merge_sha=sha)
        analysis = self.changes.issues(chg, Stage.SYSTEM_ANALYSIS)
        parent = analysis[0]["issue_key"] if analysis else None
        spec_url = self.bitbucket.browse_url(ev.project_key, ev.repository_slug, path, sha)
        component = spec.metadata.get("component", "")
        done_criteria = spec.sections.get("критерии готовности")

        keys: dict[str, str] = {}
        created, reconciled = [], []
        for task in spec.tasks:
            description_lines = [
                f"Реализовать задачу {task.ref} из функциональной спецификации.",
                f"Change ID: {chg}",
                f"Component: {component}",
                f"Functional specification: {spec_url}",
                f"Specification commit: {sha}",
                f"System analysis: {parent or '-'}",
                f"Ветка реализации: feature/{chg}-<JIRA-KEY>-implementation",
            ]
            if task.depends_on:
                description_lines.append(f"Зависит от: {', '.join(task.depends_on)}")
            if done_criteria:
                description_lines += ["", "Критерии готовности:", done_criteria.body]
            ensured = self.idempotency.ensure_issue(
                operation_key=f"{operation_key}:{task.ref}", change_id=chg, stage=Stage.DEVELOPMENT,
                project=self.settings.jira_dev_project, issue_type=self.settings.jira_dev_issue_type,
                summary=f"[SDD] {chg} · {task.ref}: {task.title}", description="\n".join(description_lines),
                parent_issue=parent, spec_ref=task.ref, extra_fields=self.settings.extra_fields("dev"),
            )
            keys[task.ref] = ensured.key
            if ensured.outcome == "created":
                created.append(ensured.key)
            elif ensured.outcome == "reconciled":
                reconciled.append(ensured.key)
            if parent:
                self.idempotency.ensure_link(self.settings.jira_relates_link_type, inward=ensured.key, outward=parent)

        for task in spec.tasks:
            for blocker in task.depends_on:
                # Jira renders this as "<inwardIssue> blocks <outwardIssue>".
                self.idempotency.ensure_link(self.settings.jira_blocks_link_type,
                                             inward=keys[blocker], outward=keys[task.ref])

        self._advance(chg, Stage.DEVELOPMENT, "in_progress")
        self._comment_pr(ev, "SDD: функциональная спецификация согласована. Созданы задачи разработки:", [
            f"- {ref} → {self._issue_ref(key)}" for ref, key in keys.items()
        ])
        return {"change_id": chg, "issues": list(keys.values()), "tasks": keys,
                "created_issues": created, "reconciled_issues": reconciled}

    def _on_implementation_merged(self, ev: NormalizedEvent, pr: dict, sha: str, operation_key: str) -> dict:
        chg, key = ev.change_id, ev.jira_key
        issue = self.changes.issue(key)
        if issue is None or issue["change_id"] != chg or issue["stage"] != Stage.DEVELOPMENT:
            raise Rejected(f"{key} не является задачей разработки изменения {chg}")
        if self.settings.require_ci:
            commit = pr.get("fromRef", {}).get("latestCommit") or ev.latest_commit
            builds = self.bitbucket.get_build_statuses(commit)
            states = [build.get("state") for build in builds]
            if not states:
                raise NotReady(f"CI ещё не сообщил статус для commit {commit[:12]}")
            if "FAILED" in states:
                raise Rejected(f"CI не пройден для commit {commit[:12]}")
            if any(state != "SUCCESSFUL" for state in states):
                raise NotReady(f"CI ещё выполняется для commit {commit[:12]}")

        self._save_artifact(ev, pr, ArtifactType.IMPLEMENTATION, state="MERGED", merge_sha=sha)
        url = pull_request_url(pr) or ev.pull_request_url
        if url:
            self.jira.add_remote_link(key, url=url, title=f"PR #{ev.pull_request_id} (merged)",
                                      global_id=f"sdd-pr-{ev.repository}-{ev.pull_request_id}")
        transitioned = None
        if issue["state"] != "done":
            self.jira.add_comment(key, f"Код слит в {pr.get('toRef', {}).get('displayId')}: {url or '-'}\n"
                                       f"Merge commit: {sha}\nChange ID: {chg}")
            transitioned = self._transition_done(key)
        self.changes.mark_issue_done(key)
        remaining = [i["issue_key"] for i in self.changes.issues(chg, Stage.DEVELOPMENT) if i["state"] != "done"]
        if remaining:
            self._advance(chg, Stage.DEVELOPMENT, "in_progress")
        else:
            self._advance(chg, Stage.DONE, "done")
        return {"change_id": chg, "issue": key, "transition": transitioned, "remaining_issues": remaining}

    # --- helpers ----------------------------------------------------------------------------------

    _DONE_ALIASES = {"done", "готово", "resolve", "resolve issue", "closed", "close", "закрыть", "resolved"}

    def _transition_done(self, key: str) -> str | None:
        name = self.settings.jira_done_transition
        if not name:
            return None
        wanted = {name.lower()}
        if name.lower() in self._DONE_ALIASES:
            wanted |= self._DONE_ALIASES
        for transition in self.jira.get_transitions(key):
            if transition.get("name", "").lower() in wanted:
                self.jira.transition(key, transition["id"])
                return transition["name"]
        log.warning("transition %r is not available for %s", name, key)
        return None

    def _spec_path(self, artifact: ArtifactType, change_id: str) -> str:
        template = (self.settings.business_spec_path if artifact is ArtifactType.BUSINESS_SPECIFICATION
                    else self.settings.functional_spec_path)
        return template.format(change_id=change_id)

    def _save_artifact(self, ev: NormalizedEvent, pr: dict, artifact: ArtifactType, *, state: str,
                       commit: str | None = None, merge_sha: str | None = None) -> None:
        self.changes.upsert_artifact(
            change_id=ev.change_id, artifact_type=artifact, repository=ev.repository,
            branch_name=ev.source_branch, pull_request_id=ev.pull_request_id,
            pull_request_url=pull_request_url(pr) or ev.pull_request_url, state=state,
            commit_sha=commit or pr.get("fromRef", {}).get("latestCommit"), merge_commit_sha=merge_sha,
            jira_key=ev.jira_key,
        )

    def _advance(self, change_id: str, stage: Stage | None, status: str, title: str | None = None) -> None:
        change = self.changes.get(change_id)
        if change is None:
            return
        current = Stage(change["stage"])
        if stage is not None and STAGE_ORDER.index(stage) < STAGE_ORDER.index(current):
            return
        self.changes.set_state(change_id, stage=stage, status=status, title=title)

    def _issue_ref(self, key: str) -> str:
        return f"{key} {self.jira.browse_url(self.settings.jira_browse_base, key)}"

    def _comment_pr(self, ev: NormalizedEvent, headline: str, lines: list[str]) -> None:
        try:
            self.bitbucket.add_pull_request_comment(
                ev.project_key, ev.repository_slug, ev.pull_request_id, "\n".join([headline, *lines])
            )
        except IntegrationError as exc:
            log.warning("cannot comment PR %s#%s: %s", ev.repository, ev.pull_request_id, exc)

    def recheck_pull_request(self, project_key: str, repository_slug: str, pull_request_id: int) -> dict:
        """Manual re-check (section 11): rebuild the event from the actual PR state and run it again."""
        from ..api.webhook_handler import normalize_pull_request

        pr = self.bitbucket.get_pull_request(project_key, repository_slug, pull_request_id)
        event_type = {"MERGED": EventType.PR_MERGED, "DECLINED": EventType.PR_DECLINED}.get(
            pr.get("state"), EventType.PR_UPDATED
        )
        ev = normalize_pull_request(
            pr, event_type=event_type, event_id=f"manual-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
            prefix=self.settings.change_id_prefix, occurred_at=None,
        )
        return self.accept(ev)


def _strip_prefix(title: str) -> str:
    for prefix in ("Бизнес-спецификация:", "Функциональная спецификация:"):
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
