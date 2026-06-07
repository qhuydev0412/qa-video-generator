import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.job import CheckpointType, JobState, JobStatus
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)

CHECKPOINT_EXPIRATION_HOURS = 24


class CheckpointError(Exception):
    def __init__(self, message: str, job_id: str | None = None):
        super().__init__(message)
        self.job_id = job_id


class NotAwaitingConfirmationError(CheckpointError):
    def __init__(self, job_id: str, current_status: JobStatus):
        self.current_status = current_status
        super().__init__(
            f"Job {job_id} is not awaiting confirmation (status: {current_status.value})",
            job_id=job_id,
        )


class WrongCheckpointError(CheckpointError):
    def __init__(self, job_id: str, expected: CheckpointType, actual: CheckpointType):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Job {job_id} is at '{actual.value}', not '{expected.value}'",
            job_id=job_id,
        )


class ConfirmationInProgressError(CheckpointError):
    def __init__(self, job_id: str):
        super().__init__(f"Confirmation already in progress for job {job_id}", job_id=job_id)


class CheckpointManager:
    def __init__(self, job_store: JobStore) -> None:
        self._store = job_store

    def pause_at_checkpoint(self, job_id: str, checkpoint_type: CheckpointType) -> None:
        now = datetime.now(timezone.utc)
        self._store.update_job(
            job_id,
            status=JobStatus.AWAITING_CONFIRMATION,
            checkpoint_type=checkpoint_type,
            checkpoint_entered_at=now,
            confirmation_lock=False,
        )
        logger.info("Job %s paused at checkpoint '%s'", job_id, checkpoint_type.value)

    def validate_and_lock(self, job_id: str, expected: CheckpointType) -> JobState:
        job = self._store.get_job(job_id)
        if job.status != JobStatus.AWAITING_CONFIRMATION:
            raise NotAwaitingConfirmationError(job_id=job_id, current_status=job.status)
        if job.checkpoint_type != expected:
            raise WrongCheckpointError(job_id=job_id, expected=expected, actual=job.checkpoint_type)  # type: ignore[arg-type]
        if job.confirmation_lock:
            raise ConfirmationInProgressError(job_id=job_id)
        self._store.update_job(job_id, confirmation_lock=True)
        return self._store.get_job(job_id)

    def release_and_resume(self, job_id: str) -> None:
        self._store.update_job(
            job_id,
            status=JobStatus.PROCESSING,
            confirmation_lock=False,
            checkpoint_type=None,
            checkpoint_entered_at=None,
        )
        logger.info("Job %s released from checkpoint, resuming", job_id)

    def expire_old_jobs(self) -> list[str]:
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(hours=CHECKPOINT_EXPIRATION_HOURS)
        expired = []
        for job_id in self._store.list_awaiting_job_ids():
            try:
                job = self._store.get_job(job_id)
            except KeyError:
                continue
            if job.checkpoint_entered_at and job.checkpoint_entered_at < threshold:
                self._store.update_job(job_id, status=JobStatus.EXPIRED)
                work_dir = Path(job.work_dir)
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
                self._store.delete_job(job_id)
                expired.append(job_id)
                logger.info("Expired job %s", job_id)
        return expired
