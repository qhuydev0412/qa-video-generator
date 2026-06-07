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
        gifs_bucket: str,
        sounds_bucket: str,
        n: int = 3,
    ) -> MediaOptions:
        """Pick n random assets from each bucket and return with presigned URLs."""
        bg_keys = self._minio.random_pick(backgrounds_bucket, n)
        gif_keys = self._minio.random_pick(gifs_bucket, n)
        sound_keys = self._minio.random_pick(sounds_bucket, n)

        return MediaOptions(
            background_options=self._to_media_list(bg_keys, backgrounds_bucket, "background"),
            gif_options=self._to_media_list(gif_keys, gifs_bucket, "gif"),
            sound_options=self._to_media_list(sound_keys, sounds_bucket, "sound"),
        )

    def _to_media_list(
        self, keys: list[str], bucket: str, media_type: str
    ) -> list[MinioMedia]:
        result = []
        for key in keys:
            url = self._minio.presigned_url(bucket, key)
            result.append(
                MinioMedia(bucket=bucket, key=key, media_type=media_type, preview_url=url)
            )
        return result
