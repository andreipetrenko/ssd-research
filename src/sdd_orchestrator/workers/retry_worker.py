import logging
import threading

from ..repositories.events import EventRepository
from ..services.workflow_service import WorkflowService
from ..settings import Settings

log = logging.getLogger(__name__)


class RetryWorker:
    """Re-processes failed events with exponential backoff and recovers events stuck in 'processing'."""

    def __init__(self, workflow: WorkflowService, events: EventRepository, settings: Settings):
        self.workflow = workflow
        self.events = events
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[dict]:
        results = []
        for event_id in self.events.due_for_retry(self.settings.stale_processing_seconds):
            results.append(self.workflow.process(event_id))
        return results

    def _loop(self) -> None:
        log.info("retry worker started (poll every %ss)", self.settings.retry_poll_seconds)
        while not self._stop.wait(self.settings.retry_poll_seconds):
            try:
                for result in self.run_once():
                    log.info("retry: event %s → %s", result.get("event_id"), result.get("status"))
            except Exception:  # noqa: BLE001 - keep the worker alive on DB hiccups
                log.exception("retry worker iteration failed")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sdd-retry-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
