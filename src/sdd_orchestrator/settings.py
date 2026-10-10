import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SDD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sdd:sdd@127.0.0.1:5432/sdd"
    db_pool_size: int = Field(default=5, ge=1)
    auto_migrate: bool = True
    migrations_dir: Path = PROJECT_ROOT / "migrations"

    host: str = "127.0.0.1"
    port: int = 48080
    public_url: str = "http://127.0.0.1:48080"

    change_id_prefix: str = "CHG"

    bitbucket_base_url: str = "http://127.0.0.1:48090"
    bitbucket_token: str = ""
    bitbucket_username: str = ""
    bitbucket_password: str = ""
    # HMAC secret configured on the Bitbucket webhook; empty disables signature checks.
    bitbucket_webhook_secret: str = ""
    spec_repositories: str = "SPEC/business-specs"
    code_repositories: str = "APP/application-repo"
    target_branches: str = "main,master"
    business_spec_path: str = "openspec/changes/{change_id}/business-spec.md"
    functional_spec_path: str = "openspec/changes/{change_id}/functional-spec.md"
    min_approvals: int = 1
    require_ci: bool = True

    jira_base_url: str = "http://127.0.0.1:48090"
    jira_browse_url: str = ""
    jira_token: str = ""
    jira_username: str = ""
    jira_password: str = ""
    jira_idea_issue_type: str = "Business Idea"
    jira_idea_required_fields: str = "summary,description"
    jira_analysis_project: str = "ANL"
    jira_analysis_issue_type: str = "Task"
    jira_dev_project: str = "DEV"
    jira_dev_issue_type: str = "Task"
    jira_relates_link_type: str = "Relates"
    jira_blocks_link_type: str = "Blocks"
    # Transition applied to a development task after its code PR is merged; empty disables it.
    jira_done_transition: str = "Done"
    # JSON objects merged into "fields" of created issues (custom fields, components, etc.).
    jira_analysis_extra_fields: str = "{}"
    jira_dev_extra_fields: str = "{}"

    http_timeout_seconds: float = 15.0

    run_retry_worker: bool = True
    retry_poll_seconds: float = 5.0
    retry_base_delay_seconds: float = 10.0
    retry_max_delay_seconds: float = 600.0
    max_attempts: int = 6
    stale_processing_seconds: float = 300.0

    log_level: str = "INFO"

    @property
    def spec_repository_list(self) -> list[str]:
        return _split(self.spec_repositories)

    @property
    def code_repository_list(self) -> list[str]:
        return _split(self.code_repositories)

    @property
    def target_branch_list(self) -> list[str]:
        return _split(self.target_branches)

    @property
    def idea_required_field_list(self) -> list[str]:
        return _split(self.jira_idea_required_fields)

    @property
    def jira_browse_base(self) -> str:
        return (self.jira_browse_url or self.jira_base_url).rstrip("/")

    def extra_fields(self, kind: str) -> dict:
        raw = self.jira_analysis_extra_fields if kind == "analysis" else self.jira_dev_extra_fields
        value = json.loads(raw or "{}")
        if not isinstance(value, dict):
            raise ValueError(f"SDD_JIRA_{kind.upper()}_EXTRA_FIELDS must be a JSON object")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
