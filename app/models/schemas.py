from typing import Optional

from pydantic import BaseModel

from app.models.job import (
    CheckpointType,
    ErrorDetail,
    ExtractedText,
    JobStatus,
    MediaOptions,
    PipelineStep,
    SegmentVoiceOptions,
)


class CreateJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_step: Optional[str] = None
    progress_percent: int
    checkpoint_type: Optional[str] = None

    # Checkpoint payloads (populated at each review step)
    extracted_texts: Optional[list[ExtractedText]] = None
    voice_options_per_segment: Optional[list[SegmentVoiceOptions]] = None
    media_options: Optional[MediaOptions] = None

    output_url: Optional[str] = None
    error: Optional[ErrorDetail] = None


class TextEdit(BaseModel):
    image_index: int
    text: str


class TextConfirmRequest(BaseModel):
    texts: list[TextEdit]


class VoiceSelection(BaseModel):
    image_index: int
    voice_id: str


class VoiceConfirmRequest(BaseModel):
    voices: list[VoiceSelection]


class MediaConfirmRequest(BaseModel):
    background_bucket: Optional[str] = None
    background_key: Optional[str] = None
    gif_bucket: Optional[str] = None
    gif_key: Optional[str] = None
    sound_bucket: Optional[str] = None
    sound_key: Optional[str] = None


class CancelResponse(BaseModel):
    job_id: str
    status: str


class ErrorResponse(BaseModel):
    error: str
    message: str
