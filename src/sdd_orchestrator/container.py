from dataclasses import dataclass

import httpx
from sqlalchemy import Engine

from .db import make_engine
from .repositories.changes import ChangeRepository
from .repositories.events import EventRepository
from .services.bitbucket_client import BitbucketClient
from .services.jira_client import JiraClient
from .services.workflow_service import WorkflowService
from .settings import Settings


@dataclass
class Services:
    settings: Settings
    engine: Engine
    jira: JiraClient
    bitbucket: BitbucketClient
    changes: ChangeRepository
    events: EventRepository
    workflow: WorkflowService


def build_services(settings: Settings, *, engine: Engine | None = None, jira_http: httpx.Client | None = None,
                   bitbucket_http: httpx.Client | None = None) -> Services:
    engine = engine or make_engine(settings.database_url, settings.db_pool_size)
    jira = JiraClient(
        settings.jira_base_url, jira_http, token=settings.jira_token, username=settings.jira_username,
        password=settings.jira_password, timeout=settings.http_timeout_seconds,
    )
    bitbucket = BitbucketClient(
        settings.bitbucket_base_url, bitbucket_http, token=settings.bitbucket_token,
        username=settings.bitbucket_username, password=settings.bitbucket_password,
        timeout=settings.http_timeout_seconds,
    )
    changes, events = ChangeRepository(engine), EventRepository(engine)
    workflow = WorkflowService(settings, jira, bitbucket, changes, events)
    return Services(settings, engine, jira, bitbucket, changes, events, workflow)
