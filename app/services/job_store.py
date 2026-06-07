import logging
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.config import settings
from app.models.job import JobState, JobStatus

logger = logging.getLogger(__name__)

JOB_KEY_PREFIX = "qavg:job:"
ACTIVE_JOBS_KEY_PREFIX = "qavg:jobs:active:"
ACTIVE_STATUSES = {JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.AWAITING_CONFIRMATION}


class JobStoreError(Exception):
    def __init__(self, message: str, original: Exception | None = None):
        super().__init__(message)
        self.original = original


class JobStore:
    def __init__(self, redis_url: str | None = None) -> None:
        url = redis_url or settings.REDIS_URL
        try:
            self._redis = redis.Redis.from_url(url, decode_responses=True)
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to connect to Redis: {exc}", original=exc)

    def create_job(self, job_id: str, work_dir: str, images: list) -> JobState:
        now = datetime.now(timezone.utc)
        job = JobState(
            job_id=job_id,
            status=JobStatus.QUEUED,
            images=images,
            work_dir=work_dir,
            created_at=now,
            updated_at=now,
        )
        try:
            self._redis.set(self._job_key(job_id), job.model_dump_json())
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to create job {job_id}: {exc}", original=exc)
        logger.info("Created job %s", job_id)
        return job

    def get_job(self, job_id: str) -> JobState:
        try:
            data = self._redis.get(self._job_key(job_id))
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to read job {job_id}: {exc}", original=exc)
        if data is None:
            raise KeyError(f"Job not found: {job_id}")
        return JobState.model_validate_json(data)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        job = self.get_job(job_id)
        for field_name, value in kwargs.items():
            setattr(job, field_name, value)
        job.updated_at = datetime.now(timezone.utc)
        try:
            self._redis.set(self._job_key(job_id), job.model_dump_json())
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to update job {job_id}: {exc}", original=exc)

    def delete_job(self, job_id: str) -> None:
        try:
            self._redis.delete(self._job_key(job_id))
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to delete job {job_id}: {exc}", original=exc)

    def list_awaiting_job_ids(self) -> list[str]:
        awaiting: list[str] = []
        cursor = 0
        try:
            while True:
                cursor, keys = self._redis.scan(cursor=cursor, match=f"{JOB_KEY_PREFIX}*", count=100)
                for key in keys:
                    raw = self._redis.get(key)
                    if raw is None:
                        continue
                    try:
                        job = JobState.model_validate_json(raw)
                        if job.status == JobStatus.AWAITING_CONFIRMATION:
                            awaiting.append(job.job_id)
                    except Exception:
                        pass
                if cursor == 0:
                    break
        except redis.RedisError as exc:
            raise JobStoreError(f"Failed to scan jobs: {exc}", original=exc)
        return awaiting

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"
