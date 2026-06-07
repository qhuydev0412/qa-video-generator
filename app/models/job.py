from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CheckpointType(str, Enum):
    TEXT_REVIEW = "text_review"
    VOICE_SELECTION = "voice_selection"
    MEDIA_SELECTION = "media_selection"


class PipelineStep(str, Enum):
    EXTRACTING_TEXT = "extracting_text"
    GENERATING_VOICES = "generating_voices"
    SELECTING_MEDIA = "selecting_media"
    COMPOSING_VIDEO = "composing_video"


class ImageType(str, Enum):
    QUESTION = "question"
    ANSWER = "answer"


class ImageInfo(BaseModel):
    index: int
    image_type: ImageType
    path: str
    filename: str


class ExtractedText(BaseModel):
    image_index: int
    text: str
    confirmed: bool = False


class VoicePreview(BaseModel):
    voice_id: str
    voice_name: str
    audio_path: str


class SegmentVoiceOptions(BaseModel):
    image_index: int
    text: str
    options: list[VoicePreview]
    selected_voice_id: Optional[str] = None
    selected_audio_path: Optional[str] = None


class MinioMedia(BaseModel):
    bucket: str
    key: str
    media_type: str
    preview_url: str = ""


class MediaOptions(BaseModel):
    background_options: list[MinioMedia] = Field(default_factory=list)
    n_transitions: int = 0


class MediaSelection(BaseModel):
    background: Optional[MinioMedia] = None
    transitions: list[MinioMedia] = Field(default_factory=list)  # one per transition slot


class ErrorDetail(BaseModel):
    step: PipelineStep
    message: str
    retryable: bool


class JobState(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    current_step: Optional[PipelineStep] = None
    progress_percent: int = Field(default=0, ge=0, le=100)

    images: list[ImageInfo] = Field(default_factory=list)
    extracted_texts: list[ExtractedText] = Field(default_factory=list)
    voice_options_per_segment: list[SegmentVoiceOptions] = Field(default_factory=list)
    media_options: Optional[MediaOptions] = None
    media_selection: Optional[MediaSelection] = None

    output_video_path: Optional[str] = None
    error: Optional[ErrorDetail] = None

    checkpoint_type: Optional[CheckpointType] = None
    checkpoint_entered_at: Optional[datetime] = None
    confirmation_lock: bool = False

    work_dir: str
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None
