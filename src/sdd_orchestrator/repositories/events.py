import json
from datetime import datetime

from sqlalchemy import Engine, text

from .changes import _row, _rows


class EventRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def register(self, *, idempotency_key: str, event_type: str, change_id: str | None, payload: dict,
                 delivery_id: str | None, source_event_at: datetime | None,
                 status: str = "received", last_error: str | None = None) -> tuple[dict, bool]:
        """Insert the event once per idempotency key. Returns (event, created)."""
        with self.engine.begin() as conn:
            created = _row(conn.execute(
                text(
                    "INSERT INTO sdd_event (idempotency_key, event_type, change_id, status, payload,"
                    " delivery_id, source_event_at, last_error)"
                    " VALUES (:key, :type, :change, :status, CAST(:payload AS JSONB), :delivery, :at, :error)"
                    " ON CONFLICT (idempotency_key) DO NOTHING RETURNING *"
                ),
                {
                    "key": idempotency_key, "type": event_type, "change": change_id, "status": status,
                    "payload": json.dumps(payload, ensure_ascii=False), "delivery": delivery_id,
                    "at": source_event_at, "error": last_error,
                },
            ))
            if created:
                return created, True
            existing = _row(conn.execute(
                text("SELECT * FROM sdd_event WHERE idempotency_key = :key"), {"key": idempotency_key}
            ))
            return existing, False

    def record_duplicate(self, event_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE sdd_event SET duplicate_count = duplicate_count + 1 WHERE id = :id"), {"id": event_id}
            )

    def claim(self, event_id: int, *, force: bool = False, stale_seconds: float = 300) -> dict | None:
        """Atomically move an event to 'processing'. Returns None if another worker owns it."""
        allowed = "('received', 'failed', 'dead', 'rejected')" if force else "('received', 'failed')"
        with self.engine.begin() as conn:
            return _row(conn.execute(
                text(
                    "UPDATE sdd_event SET status = 'processing', locked_at = now(), attempts = attempts + 1"
                    f" WHERE id = :id AND (status IN {allowed}"
                    "  OR (status = 'processing' AND locked_at < now() - make_interval(secs => :stale)))"
                    " RETURNING *"
                ),
                {"id": event_id, "stale": stale_seconds},
            ))

    def mark_done(self, event_id: int, result: dict, change_id: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sdd_event SET status = 'done', result = CAST(:result AS JSONB), last_error = NULL,"
                    " next_retry_at = NULL, locked_at = NULL, processed_at = now(),"
                    " change_id = COALESCE(:change, change_id) WHERE id = :id"
                ),
                {"id": event_id, "result": json.dumps(result, ensure_ascii=False), "change": change_id},
            )

    def mark_rejected(self, event_id: int, reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sdd_event SET status = 'rejected', last_error = :reason, next_retry_at = NULL,"
                    " locked_at = NULL, processed_at = now() WHERE id = :id"
                ),
                {"id": event_id, "reason": reason},
            )

    def mark_failed(self, event_id: int, error: str, *, delay_seconds: float, dead: bool) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE sdd_event SET status = :status, last_error = :error, locked_at = NULL,"
                    " next_retry_at = CASE WHEN :dead THEN NULL ELSE now() + make_interval(secs => :delay) END"
                    " WHERE id = :id"
                ),
                {"id": event_id, "error": error[:4000], "delay": delay_seconds, "dead": dead,
                 "status": "dead" if dead else "failed"},
            )

    def due_for_retry(self, stale_seconds: float, limit: int = 20) -> list[int]:
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(
                text(
                    "SELECT id FROM sdd_event WHERE (status = 'failed' AND next_retry_at <= now())"
                    " OR (status = 'processing' AND locked_at < now() - make_interval(secs => :stale))"
                    " ORDER BY next_retry_at NULLS FIRST, id LIMIT :limit"
                ),
                {"stale": stale_seconds, "limit": limit},
            )]

    def get(self, event_id: int) -> dict | None:
        with self.engine.connect() as conn:
            return _row(conn.execute(text("SELECT * FROM sdd_event WHERE id = :id"), {"id": event_id}))

    def recent(self, *, change_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        clauses, params = [], {"limit": limit}
        if change_id:
            clauses.append("change_id = :change")
            params["change"] = change_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.engine.connect() as conn:
            return _rows(conn.execute(
                text(f"SELECT * FROM sdd_event {where} ORDER BY received_at DESC, id DESC LIMIT :limit"), params
            ))

    def metrics(self) -> dict:
        with self.engine.connect() as conn:
            counts = {row[0]: row[1] for row in conn.execute(
                text("SELECT status, count(*) FROM sdd_event GROUP BY status")
            )}
            totals = _row(conn.execute(text(
                "SELECT count(*) + COALESCE(sum(duplicate_count), 0) AS deliveries,"
                " COALESCE(sum(duplicate_count), 0) AS duplicates,"
                " COALESCE(sum(GREATEST(attempts - 1, 0)), 0) AS retries FROM sdd_event"
            )))
            latency = _row(conn.execute(text(
                "SELECT avg(EXTRACT(EPOCH FROM processed_at - source_event_at)) AS avg_seconds,"
                " max(EXTRACT(EPOCH FROM processed_at - source_event_at)) AS max_seconds"
                " FROM sdd_event WHERE status = 'done' AND event_type = 'pull_request_merged'"
                " AND source_event_at IS NOT NULL AND result ? 'created_issues'"
            )))
            idea_to_pr = _row(conn.execute(text(
                "SELECT avg(EXTRACT(EPOCH FROM first_pr - c.created_at)) AS avg_seconds FROM sdd_change c"
                " JOIN (SELECT change_id, min(created_at) AS first_pr FROM sdd_artifact"
                "       WHERE artifact_type = 'implementation' GROUP BY change_id) a USING (change_id)"
            )))
            first_review = _row(conn.execute(text(
                "SELECT count(*) FILTER (WHERE NOT reworked) AS first_pass, count(*) AS total FROM ("
                " SELECT a.id, EXISTS (SELECT 1 FROM sdd_event e WHERE e.event_type = 'pull_request_needs_work'"
                "   AND e.payload->>'repository_slug' = split_part(a.repository, '/', 2)"
                "   AND (e.payload->>'pull_request_id')::bigint = a.pull_request_id) AS reworked"
                " FROM sdd_artifact a WHERE a.pull_request_state = 'MERGED') t"
            )))
        return {
            "events_by_status": counts,
            "deliveries": int(totals["deliveries"]),
            "duplicates_prevented": int(totals["duplicates"]),
            "retries": int(totals["retries"]),
            "merge_to_jira_avg_seconds": _float(latency["avg_seconds"]),
            "merge_to_jira_max_seconds": _float(latency["max_seconds"]),
            "idea_to_code_pr_avg_seconds": _float(idea_to_pr["avg_seconds"]),
            "merged_prs": int(first_review["total"]),
            "merged_prs_first_review": int(first_review["first_pass"]),
        }


def _float(value) -> float | None:
    return float(value) if value is not None else None
