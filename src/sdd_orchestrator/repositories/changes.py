from sqlalchemy import Engine, text


def _rows(result) -> list[dict]:
    return [dict(row._mapping) for row in result]


def _row(result) -> dict | None:
    row = result.first()
    return dict(row._mapping) if row else None


class ChangeRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def allocate(self, prefix: str, source_issue: str, title: str | None) -> tuple[dict, bool]:
        """Create a change for a Business Idea or return the existing one (idempotent by source issue)."""
        with self.engine.begin() as conn:
            existing = _row(conn.execute(
                text("SELECT * FROM sdd_change WHERE source_issue = :s"), {"s": source_issue}
            ))
            if existing:
                return existing, False
            number = conn.execute(text("SELECT nextval('sdd_change_number_seq')")).scalar_one()
            created = _row(conn.execute(
                text(
                    "INSERT INTO sdd_change (change_id, source_issue, title, stage, status)"
                    " VALUES (:id, :s, :t, 'idea', 'registered')"
                    " ON CONFLICT (source_issue) DO NOTHING RETURNING *"
                ),
                {"id": f"{prefix}-{number}", "s": source_issue, "t": title},
            ))
            if created:
                return created, True
            return _row(conn.execute(
                text("SELECT * FROM sdd_change WHERE source_issue = :s"), {"s": source_issue}
            )), False

    def ensure(self, change_id: str, source_issue: str, title: str | None, stage: str, status: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sdd_change (change_id, source_issue, title, stage, status)"
                    " VALUES (:id, :s, :t, :stage, :status) ON CONFLICT (change_id) DO NOTHING"
                ),
                {"id": change_id, "s": source_issue, "t": title, "stage": stage, "status": status},
            )
            return _row(conn.execute(text("SELECT * FROM sdd_change WHERE change_id = :id"), {"id": change_id}))

    def get(self, change_id: str) -> dict | None:
        with self.engine.connect() as conn:
            return _row(conn.execute(text("SELECT * FROM sdd_change WHERE change_id = :id"), {"id": change_id}))

    def recent(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as conn:
            return _rows(conn.execute(
                text("SELECT * FROM sdd_change ORDER BY updated_at DESC LIMIT :l"), {"l": limit}
            ))

    def set_state(self, change_id: str, *, stage: str | None = None, status: str | None = None,
                  title: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sdd_change SET stage = COALESCE(:stage, stage), status = COALESCE(:status, status),"
                    " title = COALESCE(title, :title), updated_at = now() WHERE change_id = :id"
                ),
                {"id": change_id, "stage": stage, "status": status, "title": title},
            )

    # --- artifacts -------------------------------------------------------------------------------

    def upsert_artifact(self, *, change_id: str, artifact_type: str, repository: str, branch_name: str,
                        pull_request_id: int, pull_request_url: str | None, state: str,
                        commit_sha: str | None, merge_commit_sha: str | None = None,
                        jira_key: str | None = None) -> dict:
        with self.engine.begin() as conn:
            return _row(conn.execute(
                text(
                    "INSERT INTO sdd_artifact (change_id, artifact_type, repository, branch_name,"
                    " pull_request_id, pull_request_url, pull_request_state, commit_sha, merge_commit_sha, jira_key)"
                    " VALUES (:change_id, :artifact_type, :repository, :branch, :pr, :url, :state, :sha,"
                    " :merge_sha, :jira)"
                    " ON CONFLICT (repository, pull_request_id) DO UPDATE SET"
                    " pull_request_state = EXCLUDED.pull_request_state,"
                    " pull_request_url = COALESCE(EXCLUDED.pull_request_url, sdd_artifact.pull_request_url),"
                    " commit_sha = COALESCE(EXCLUDED.commit_sha, sdd_artifact.commit_sha),"
                    " merge_commit_sha = COALESCE(EXCLUDED.merge_commit_sha, sdd_artifact.merge_commit_sha),"
                    " jira_key = COALESCE(EXCLUDED.jira_key, sdd_artifact.jira_key),"
                    " updated_at = now()"
                    " RETURNING *"
                ),
                {
                    "change_id": change_id, "artifact_type": artifact_type, "repository": repository,
                    "branch": branch_name, "pr": pull_request_id, "url": pull_request_url, "state": state,
                    "sha": commit_sha, "merge_sha": merge_commit_sha, "jira": jira_key,
                },
            ))

    def artifacts(self, change_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            return _rows(conn.execute(
                text("SELECT * FROM sdd_artifact WHERE change_id = :id ORDER BY created_at, id"), {"id": change_id}
            ))

    def merged_artifact(self, change_id: str, artifact_type: str) -> dict | None:
        with self.engine.connect() as conn:
            return _row(conn.execute(
                text(
                    "SELECT * FROM sdd_artifact WHERE change_id = :id AND artifact_type = :t"
                    " AND pull_request_state = 'MERGED' ORDER BY updated_at DESC LIMIT 1"
                ),
                {"id": change_id, "t": artifact_type},
            ))

    # --- Jira issue links ------------------------------------------------------------------------

    def issue_by_operation(self, operation_key: str) -> dict | None:
        with self.engine.connect() as conn:
            return _row(conn.execute(
                text("SELECT * FROM sdd_issue_link WHERE operation_key = :k"), {"k": operation_key}
            ))

    def save_issue(self, *, change_id: str, issue_key: str, issue_type: str, parent_issue: str | None,
                   stage: str, operation_key: str, spec_ref: str | None, summary: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sdd_issue_link (change_id, issue_key, issue_type, parent_issue, stage,"
                    " operation_key, spec_ref, summary)"
                    " VALUES (:change_id, :key, :type, :parent, :stage, :op, :ref, :summary)"
                    " ON CONFLICT (operation_key) DO NOTHING"
                ),
                {
                    "change_id": change_id, "key": issue_key, "type": issue_type, "parent": parent_issue,
                    "stage": stage, "op": operation_key, "ref": spec_ref, "summary": summary,
                },
            )
            return _row(conn.execute(
                text("SELECT * FROM sdd_issue_link WHERE operation_key = :k"), {"k": operation_key}
            ))

    def issues(self, change_id: str, stage: str | None = None) -> list[dict]:
        query = "SELECT * FROM sdd_issue_link WHERE change_id = :id"
        if stage:
            query += " AND stage = :stage"
        with self.engine.connect() as conn:
            return _rows(conn.execute(text(query + " ORDER BY id"), {"id": change_id, "stage": stage}))

    def issue(self, issue_key: str) -> dict | None:
        with self.engine.connect() as conn:
            return _row(conn.execute(
                text("SELECT * FROM sdd_issue_link WHERE issue_key = :k"), {"k": issue_key}
            ))

    def mark_issue_done(self, issue_key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE sdd_issue_link SET state = 'done' WHERE issue_key = :k"), {"k": issue_key})
