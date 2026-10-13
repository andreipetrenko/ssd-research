from argparse import Namespace
from pathlib import Path

import pytest

from sdd_orchestrator.cli import build_parser
from sdd_orchestrator.services.idempotency_service import operation_marker, operation_token
from sdd_orchestrator.settings import Settings
from sdd_orchestrator.setup import collect_init_values, ensure_database, run_doctor, uses_local_mocks, write_env


def test_write_env_creates_minimal_file(tmp_path: Path):
    path = tmp_path / ".env"
    write_env(path, {
        "SDD_PUBLIC_URL": "https://sdd.example",
        "SDD_JIRA_BASE_URL": "https://jira.example",
        "SDD_JIRA_TOKEN": "tok",
        "SDD_BITBUCKET_BASE_URL": "https://bb.example",
        "SDD_BITBUCKET_TOKEN": "bb",
        "SDD_SPEC_REPOSITORIES": "SPEC/business-specs",
        "SDD_CODE_REPOSITORIES": "APP/application-repo",
        "SDD_JIRA_ANALYSIS_PROJECT": "ANL",
        "SDD_JIRA_DEV_PROJECT": "DEV",
        "SDD_JIRA_IDEA_ISSUE_TYPE": "Epic",
        "SDD_BITBUCKET_WEBHOOK_SECRET": "secret",
        "SDD_DATABASE_URL": "postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd",
    })
    text = path.read_text(encoding="utf-8")
    assert "SDD_PUBLIC_URL=https://sdd.example" in text
    assert "SDD_JIRA_TOKEN=tok" in text
    assert "labels" not in text.lower()


def test_doctor_passes_against_mocks(harness):
    report = run_doctor(services=harness.services, settings=harness.settings)
    failed = [item.name + ": " + item.detail for item in report.checks if not item.ok]
    assert failed == [], failed


def test_repo_webhook_is_created_once(harness):
    bitbucket = harness.services.bitbucket
    url = harness.settings.public_url + "/webhooks/bitbucket"
    first = bitbucket.ensure_repo_webhook("SPEC", "business-specs", url=url, secret="x")
    second = bitbucket.ensure_repo_webhook("SPEC", "business-specs", url=url, secret="x")
    assert first["created"] is True
    assert second["created"] is False


def test_operation_token_is_stable():
    assert operation_token("CHG-1:create-system-analysis:abc") == operation_token(
        "CHG-1:create-system-analysis:abc"
    )
    assert operation_marker("x").startswith("SDD-OP:sddop-")


def test_init_non_interactive_requires_fields():
    args = Namespace(
        public_url="", database_url="", jira_url="", jira_token="",
        bitbucket_url="", bitbucket_token="", spec_repo="", code_repo="",
        target_branch="master", idea_type="", analysis_project="", analysis_type="Task",
        dev_project="", dev_type="Task", done_transition="Done", webhook_secret="",
        no_ci=False, non_interactive=True,
    )
    with pytest.raises(SystemExit, match="не хватает"):
        collect_init_values(args)


def test_init_non_interactive_accepts_complete_args():
    args = Namespace(
        public_url="https://sdd.example", database_url="postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd",
        jira_url="https://jira.example", jira_token="j", bitbucket_url="https://bb.example",
        bitbucket_token="b", spec_repo="SPEC/specs", code_repo="APP/app", target_branch="master",
        idea_type="Epic", analysis_project="ANL", analysis_type="Task", dev_project="DEV",
        dev_type="Story", done_transition="Done", webhook_secret="secret", no_ci=True,
        non_interactive=True,
    )
    values = collect_init_values(args)
    assert values["SDD_REQUIRE_CI"] == "false"
    assert values["SDD_BITBUCKET_WEBHOOK_SECRET"] == "secret"


def test_default_settings_use_local_mocks():
    settings = Settings(_env_file=None)
    assert uses_local_mocks(settings)
    assert not uses_local_mocks(Settings(_env_file=None, jira_base_url="https://jira.company.ru",
                                         bitbucket_base_url="https://bitbucket.company.ru"))


def test_ensure_database_when_already_up():
    assert ensure_database("postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd").startswith("PostgreSQL")


def test_up_is_the_default_launch_command():
    help_text = build_parser().format_help()
    assert "sdd up" in help_text or "up" in help_text
    ns = build_parser().parse_args(["up"])
    assert ns.func.__name__ == "cmd_up"