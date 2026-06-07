import logging

from app.models.job import MediaOptions, MinioMedia
from app.services.minio_client import MinioClient

logger = logging.getLogger(__name__)


class MediaSelector:
    def __init__(self, minio: MinioClient) -> None:
        self._minio = minio

    def pick_options(
        self,
        backgrounds_bucket: str,
        n_transitions: int,
        n_backgrounds: int = 3,
    ) -> MediaOptions:
        bg_keys = self._minio.random_pick(backgrounds_bucket, n_backgrounds)
        return MediaOptions(
            background_options=self._to_media_list(bg_keys, backgrounds_bucket, "background"),
            n_transitions=n_transitions,
        )

    def _to_media_list(self, keys: list[str], bucket: str, media_type: str) -> list[MinioMedia]:
        return [
            MinioMedia(bucket=bucket, key=key, media_type=media_type)
            for key in keys
        ]
