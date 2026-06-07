from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    STORAGE_PATH: str = "storage/jobs"

    OPENAI_API_KEY: str = ""
    TTS_MODEL: str = "tts-1"
    OCR_MODEL: str = "gpt-4o"

    MINIO_ENDPOINT: str = "minio-i-new.zeabur.app"
    MINIO_ACCESS_KEY: str = "minio"
    MINIO_SECRET_KEY: str = ""
    MINIO_SECURE: bool = True

    MINIO_BUCKET_BACKGROUNDS: str = "backgrounds"
    MINIO_BUCKET_GIFS: str = "meme-gifs"
    MINIO_BUCKET_SOUNDS: str = "meme-audios"

    VIDEO_WIDTH: int = 1080
    VIDEO_HEIGHT: int = 1920
    TRANSITION_DURATION: float = 2.0
    BACKGROUND_AUDIO_VOLUME: float = 0.3

    FILE_EXPIRY_HOURS: int = 24
    MAX_CONCURRENT_JOBS: int = 3


settings = Settings()
