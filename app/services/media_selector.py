import logging
import random

from app.models.job import MediaOptions, MinioMedia
from app.services.minio_client import MinioClient

logger = logging.getLogger(__name__)


class MediaSelector:
    def __init__(self, minio: MinioClient) -> None:
        self._minio = minio

    def pick_options(
        self,
        backgrounds_bucket: str,
        gifs_bucket: str,
        sounds_bucket: str,
        n_transitions: int,
        n_backgrounds: int = 3,
    ) -> MediaOptions:
        bg_keys = self._minio.random_pick(backgrounds_bucket, n_backgrounds)

        # Build a pool with at least n_transitions items (mix of gif + sound)
        # Pick enough from each bucket; prefer even split
        n_each = max(n_transitions, 3)
        gif_keys = self._minio.random_pick(gifs_bucket, n_each)
        sound_keys = self._minio.random_pick(sounds_bucket, n_each)

        pool = (
            self._to_media_list(gif_keys, gifs_bucket, "gif")
            + self._to_media_list(sound_keys, sounds_bucket, "sound")
        )
        random.shuffle(pool)

        return MediaOptions(
            background_options=self._to_media_list(bg_keys, backgrounds_bucket, "background"),
            transition_pool=pool,
            n_transitions=n_transitions,
        )

    def _to_media_list(self, keys: list[str], bucket: str, media_type: str) -> list[MinioMedia]:
        result = []
        for key in keys:
            url = self._minio.presigned_url(bucket, key)
            result.append(MinioMedia(bucket=bucket, key=key, media_type=media_type, preview_url=url))
        return result
