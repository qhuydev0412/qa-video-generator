import logging
import random
from datetime import timedelta
from pathlib import Path

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)


class MinioClientError(Exception):
    pass


class MinioClient:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = False,
    ) -> None:
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    def list_keys(self, bucket: str) -> list[str]:
        try:
            objects = self._client.list_objects(bucket_name=bucket, recursive=True)
            return [obj.object_name for obj in objects if obj.object_name]
        except S3Error as exc:
            logger.error("MinIO list_objects failed for bucket '%s': %s", bucket, exc)
            return []

    def random_pick(self, bucket: str, n: int = 3) -> list[str]:
        keys = self.list_keys(bucket)
        if not keys:
            return []
        return random.sample(keys, min(n, len(keys)))

    def search_keys(self, bucket: str, query: str, n: int = 10) -> list[str]:
        keys = self.list_keys(bucket)
        q = query.lower()
        matched = [k for k in keys if q in Path(k).name.lower()]
        return matched[:n]

    def presigned_url(self, bucket: str, key: str, expires_seconds: int = 3600) -> str:
        try:
            return self._client.presigned_get_object(
                bucket_name=bucket,
                object_name=key,
                expires=timedelta(seconds=expires_seconds),
            )
        except S3Error as exc:
            logger.error("MinIO presigned URL failed: %s", exc)
            return ""

    def download(self, bucket: str, key: str, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.fget_object(bucket_name=bucket, object_name=key, file_path=str(dest_path))
        except S3Error as exc:
            raise MinioClientError(f"Download failed for {bucket}/{key}: {exc}") from exc

    def stream_bytes(self, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(bucket_name=bucket, object_name=key)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as exc:
            raise MinioClientError(f"Stream failed for {bucket}/{key}: {exc}") from exc

    def bucket_exists(self, bucket: str) -> bool:
        try:
            return self._client.bucket_exists(bucket_name=bucket)
        except S3Error:
            return False
