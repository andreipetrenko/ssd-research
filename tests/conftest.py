import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sdd_mocks.app import create_mock_app
from sdd_mocks.state import Delivery, MockState
from sdd_orchestrator.app import create_app
from sdd_orchestrator.container import build_services
from sdd_orchestrator.db import make_engine, run_migrations
from sdd_orchestrator.settings import PROJECT_ROOT, Settings

TEST_DB = os.environ.get("SDD_TEST_DATABASE_URL", "postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd_test")
EXAMPLES = PROJECT_ROOT / "examples" / "credit-holidays"


@pytest.fixture(scope="session")
def engine():
    engine = make_engine(TEST_DB)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL недоступна ({exc.__class__.__name__}); задайте SDD_TEST_DATABASE_URL")
    with engine.begin() as conn:
        conn.execute(text(
            "DROP TABLE IF EXISTS sdd_event, sdd_issue_link, sdd_artifact, sdd_change, sdd_schema_migrations CASCADE"
        ))
        conn.execute(text("DROP SEQUENCE IF EXISTS sdd_change_number_seq"))
    run_migrations(engine, PROJECT_ROOT / "migrations")
    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sdd_event, sdd_issue_link, sdd_artifact, sdd_change RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER SEQUENCE sdd_change_number_seq RESTART WITH 1042"))
    return engine


def example(name: str, **values: str) -> str:
    content = (EXAMPLES / name).read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    return content


class Harness:
    """Drives the mock Bitbucket/Jira and delivers their webhooks to the orchestrator synchronously."""

    def __init__(self, state: MockState, mock: TestClient, orchestrator: TestClient, services, settings):
        self.state = state
        self.mock = mock
        self.orchestrator = orchestrator
        self.services = services
        self.settings = settings
        self.pending: list[Delivery] = []
        state.dispatcher = self.pending.append

    def flush(self) -> list[dict]:
        results = []
        while self.pending:
            delivery = self.pending.pop(0)
            self.state.deliver_now(delivery, client=self.orchestrator)
            assert delivery.status_code is not None, delivery.error
            results.append({"event": delivery.event_key, "status_code": delivery.status_code,
                            "body": self.orchestrator_json(delivery)})
        return results

    def flush_event(self, event_key: str) -> dict:
        results = [r for r in self.flush() if r["event"] == event_key]
        assert results, f"webhook {event_key} не был отправлен"
        return results[-1]["body"]

    @staticmethod
    def orchestrator_json(delivery: Delivery) -> dict:
        import json

        try:
            return json.loads(delivery.response)
        except ValueError:
            return {"raw": delivery.response}

    def push(self, branch: str, files: dict[str, str], project: str = "SPEC", repo: str = "business-specs") -> str:
        response = self.mock.post("/mock/bitbucket/push", json={
            "project": project, "repo": repo, "branch": branch, "files": files, "message": f"Update {branch}",
        })
        response.raise_for_status()
        return response.json()["commit"]

    def open_pr(self, branch: str, description: str, title: str = "PR", project: str = "SPEC",
                repo: str = "business-specs") -> dict:
        response = self.mock.post(f"/rest/api/latest/projects/{project}/repos/{repo}/pull-requests", json={
            "title": title, "description": description,
            "fromRef": {"id": f"refs/heads/{branch}"}, "toRef": {"id": "refs/heads/main"},
        })
        response.raise_for_status()
        return response.json()

    def action(self, pr: dict, action: str, project: str = "SPEC", repo: str = "business-specs", **body) -> dict:
        response = self.mock.post(f"/mock/bitbucket/{project}/{repo}/pull-requests/{pr['id']}/{action}", json=body)
        response.raise_for_status()
        return response.json()

    def issues(self, project: str | None = None) -> list[dict]:
        return [i for i in self.state.issues.values()
                if project is None or i["fields"]["project"]["key"] == project]

    def comments(self, pr: dict, project: str = "SPEC", repo: str = "business-specs") -> list[str]:
        return [c["text"] for c in self.state.repo(project, repo).comments[pr["id"]]]

    # --- scenario steps ---------------------------------------------------------------------------

    def register_idea(self, key: str = "IDEA-123") -> str:
        response = self.orchestrator.post("/api/changes", json={"source_issue": key})
        assert response.status_code == 201, response.text
        return response.json()["change_id"]

    def business_spec_merged(self, idea: str = "IDEA-123") -> tuple[str, dict]:
        change_id = self.register_idea(idea)
        branch = f"feature/{change_id}-business-spec"
        path = self.settings.business_spec_path.format(change_id=change_id)
        self.push(branch, {path: example("business-spec.md", CHANGE_ID=change_id, IDEA_KEY=idea)})
        pr = self.open_pr(branch, f"Change ID: {change_id}\nStage: business_specification\nJira: {idea}",
                          title=f"[{change_id}] Бизнес-спецификация")
        self.flush()
        self.action(pr, "approve")
        self.action(pr, "merge")
        self.flush()
        return change_id, pr

    def functional_spec_merged(self, change_id: str) -> dict:
        trace = self.orchestrator.get(f"/api/changes/{change_id}").json()
        business = next(a for a in trace["artifacts"] if a["artifact_type"] == "business_specification")
        analysis = next(i for i in trace["issues"] if i["stage"] == "system_analysis")
        branch = f"feature/{change_id}-system-spec"
        path = self.settings.functional_spec_path.format(change_id=change_id)
        self.push(branch, {path: example("functional-spec.md", CHANGE_ID=change_id,
                                         BUSINESS_COMMIT=business["merge_commit_sha"],
                                         ANALYSIS_KEY=analysis["issue_key"])})
        pr = self.open_pr(branch, f"Change ID: {change_id}\nStage: functional_specification",
                          title=f"[{change_id}] Функциональная спецификация")
        self.flush()
        self.action(pr, "approve")
        self.action(pr, "merge")
        self.flush()
        return pr


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=TEST_DB,
        bitbucket_base_url="http://mock",
        jira_base_url="http://mock",
        public_url="http://orchestrator",
        retry_base_delay_seconds=0,
        run_retry_worker=False,
        migrations_dir=Path(PROJECT_ROOT / "migrations"),
    )


@pytest.fixture
def harness(clean_db, settings):
    state = MockState(public_url="http://mock", webhook_url="http://orchestrator/webhooks/bitbucket",
                      webhook_secret=settings.bitbucket_webhook_secret)
    with TestClient(create_mock_app(state), base_url="http://mock", raise_server_exceptions=False) as mock:
        services = build_services(settings, engine=clean_db, jira_http=mock, bitbucket_http=mock)
        app = create_app(settings, services, start_worker=False)
        with TestClient(app, base_url="http://orchestrator") as orchestrator:
            yield Harness(state, mock, orchestrator, services, settings)
