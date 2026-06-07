"""FastAPI routes for QA Video Generator."""

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.status import HTTP_202_ACCEPTED, HTTP_404_NOT_FOUND

from app.core.config import settings
from app.models.job import (
    CheckpointType,
    ImageInfo,
    ImageType,
    JobStatus,
    MediaSelection,
    MinioMedia,
)
from app.models.schemas import (
    CancelResponse,
    CreateJobResponse,
    ErrorResponse,
    JobStatusResponse,
    MediaConfirmRequest,
    TextConfirmRequest,
    TransitionItem,
    VoicePreviewRequest,
)
from app.services.checkpoint_manager import (
    CheckpointManager,
    ConfirmationInProgressError,
    NotAwaitingConfirmationError,
    WrongCheckpointError,
)
from app.services.job_store import JobStore
from app.services.minio_client import MinioClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["qa"])

# Module-level singletons (set by configure_routes)
_job_store: JobStore | None = None


def configure_routes(job_store: JobStore) -> None:
    global _job_store  # noqa: PLW0603
    _job_store = job_store


def _store() -> JobStore:
    if _job_store is None:
        raise RuntimeError("Routes not configured")
    return _job_store


def _checkpoint(store: JobStore = Depends(_store)) -> CheckpointManager:
    return CheckpointManager(store)


def _minio() -> MinioClient:
    return MinioClient(
        endpoint=settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------


@router.post("/jobs", response_model=CreateJobResponse, status_code=HTTP_202_ACCEPTED)
async def create_job(
    question_image: UploadFile = File(...),
    answer_images: list[UploadFile] = File(...),
    store: JobStore = Depends(_store),
) -> CreateJobResponse:
    """Upload question image + answer images to create a QA video job."""
    job_id = str(uuid.uuid4())
    work_dir = Path(settings.STORAGE_PATH) / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    images_dir = work_dir / "images"
    images_dir.mkdir()

    images: list[ImageInfo] = []

    # Save question image at index 0
    q_suffix = Path(question_image.filename or "image.jpg").suffix or ".jpg"
    q_path = images_dir / f"img_000{q_suffix}"
    with open(q_path, "wb") as f:
        f.write(await question_image.read())
    images.append(
        ImageInfo(
            index=0,
            image_type=ImageType.QUESTION,
            path=str(q_path),
            filename=question_image.filename or f"img_000{q_suffix}",
        )
    )

    # Save answer images starting at index 1
    for i, upload in enumerate(answer_images, start=1):
        suffix = Path(upload.filename or "image.jpg").suffix or ".jpg"
        img_path = images_dir / f"img_{i:03d}{suffix}"
        with open(img_path, "wb") as f:
            f.write(await upload.read())
        images.append(
            ImageInfo(
                index=i,
                image_type=ImageType.ANSWER,
                path=str(img_path),
                filename=upload.filename or f"img_{i:03d}{suffix}",
            )
        )

    store.create_job(job_id=job_id, work_dir=str(work_dir), images=images)

    from app.tasks.qa_task import extract_texts_task
    extract_texts_task.delay(job_id)

    logger.info("Created job %s with %d images", job_id, len(images))
    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED.value,
        message="Đã tạo job, đang nhận diện chữ trong ảnh...",
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, store: JobStore = Depends(_store)) -> JobStatusResponse:
    job = _get_or_404(store, job_id)

    output_url = None
    if job.status == JobStatus.COMPLETED and job.output_video_path:
        output_url = f"/api/v1/jobs/{job_id}/download"

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        current_step=job.current_step.value if job.current_step else None,
        progress_percent=job.progress_percent,
        checkpoint_type=job.checkpoint_type.value if job.checkpoint_type else None,
        extracted_texts=job.extracted_texts if job.checkpoint_type == CheckpointType.TEXT_REVIEW else None,
        voice_options_per_segment=job.voice_options_per_segment if job.checkpoint_type == CheckpointType.VOICE_SELECTION else None,
        media_options=job.media_options if job.checkpoint_type == CheckpointType.MEDIA_SELECTION else None,
        output_url=output_url,
        error=job.error,
    )


# ---------------------------------------------------------------------------
# Confirm text
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/confirm/text", response_model=CreateJobResponse)
def confirm_text(
    job_id: str,
    body: TextConfirmRequest,
    store: JobStore = Depends(_store),
    cp: CheckpointManager = Depends(_checkpoint),
) -> CreateJobResponse:
    try:
        cp.validate_and_lock(job_id, CheckpointType.TEXT_REVIEW)
    except (NotAwaitingConfirmationError, WrongCheckpointError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfirmationInProgressError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    job = store.get_job(job_id)
    old_text_map = {t.image_index: t.text for t in job.extracted_texts}
    new_text_map = {e.image_index: e.text for e in body.texts}

    texts_changed = any(new_text_map.get(k) != v for k, v in old_text_map.items())

    updated = [
        t.model_copy(update={"text": new_text_map.get(t.image_index, t.text), "confirmed": True})
        for t in job.extracted_texts
    ]
    store.update_job(job_id, extracted_texts=updated)
    cp.release_and_resume(job_id)

    # Nếu text không đổi và đã có voice options → nhảy thẳng đến voice selection
    if not texts_changed and job.voice_options_per_segment:
        store.update_job(
            job_id,
            status=JobStatus.AWAITING_CONFIRMATION,
            checkpoint_type=CheckpointType.VOICE_SELECTION,
        )
        return CreateJobResponse(
            job_id=job_id,
            status=JobStatus.AWAITING_CONFIRMATION.value,
            message="Nội dung không thay đổi, giữ nguyên giọng đọc",
        )

    from app.tasks.qa_task import generate_voices_task
    generate_voices_task.delay(job_id)
    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.PROCESSING.value,
        message="Đang tạo giọng đọc...",
    )


# ---------------------------------------------------------------------------
# Preview voice on-demand (generate a specific voice for a segment)
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/segments/{segment_idx}/preview-voice")
def preview_voice(
    job_id: str,
    segment_idx: int,
    body: VoicePreviewRequest,
    store: JobStore = Depends(_store),
) -> dict:
    from app.models.job import VoicePreview
    from app.services.voice_synthesizer import VOICES, VoiceSynthesizer

    job = _get_or_404(store, job_id)
    if job.status != JobStatus.AWAITING_CONFIRMATION or job.checkpoint_type != CheckpointType.VOICE_SELECTION:
        raise HTTPException(status_code=409, detail="Job không ở bước chọn giọng")

    seg = next((s for s in job.voice_options_per_segment if s.image_index == segment_idx), None)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment không tồn tại")

    voice_id = body.voice_id
    if voice_id not in {v["id"] for v in VOICES}:
        raise HTTPException(status_code=400, detail="voice_id không hợp lệ")

    voice_name = next(v["name"] for v in VOICES if v["id"] == voice_id)

    existing = next((o for o in seg.options if o.voice_id == voice_id), None)
    if existing:
        audio_filename = Path(existing.audio_path).name
        updated_seg = seg.model_copy(update={
            "selected_voice_id": voice_id,
            "selected_audio_path": existing.audio_path,
        })
    else:
        voices_dir = Path(job.work_dir) / "voices"
        voices_dir.mkdir(parents=True, exist_ok=True)
        output_path = voices_dir / f"img{segment_idx}_{voice_id}.mp3"
        try:
            synthesizer = VoiceSynthesizer(model=settings.TTS_MODEL)
            synthesizer.synthesize(seg.text, voice_id, output_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"TTS thất bại: {exc}")

        audio_filename = output_path.name
        new_preview = VoicePreview(voice_id=voice_id, voice_name=voice_name, audio_path=str(output_path))
        updated_seg = seg.model_copy(update={
            "options": seg.options + [new_preview],
            "selected_voice_id": voice_id,
            "selected_audio_path": str(output_path),
        })

    updated_segs = [updated_seg if s.image_index == segment_idx else s for s in job.voice_options_per_segment]
    store.update_job(job_id, voice_options_per_segment=updated_segs)

    return {"audio_filename": audio_filename, "voice_name": voice_name}


# ---------------------------------------------------------------------------
# Confirm voices
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/confirm/voices", response_model=CreateJobResponse)
def confirm_voices(
    job_id: str,
    store: JobStore = Depends(_store),
    cp: CheckpointManager = Depends(_checkpoint),
) -> CreateJobResponse:
    try:
        cp.validate_and_lock(job_id, CheckpointType.VOICE_SELECTION)
    except (NotAwaitingConfirmationError, WrongCheckpointError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfirmationInProgressError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    cp.release_and_resume(job_id)

    from app.tasks.qa_task import select_media_task
    select_media_task.delay(job_id)
    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.PROCESSING.value,
        message="Đang tìm media...",
    )


# ---------------------------------------------------------------------------
# Regenerate media options
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/regenerate/media", response_model=CreateJobResponse)
def regenerate_media(
    job_id: str,
    store: JobStore = Depends(_store),
    cp: CheckpointManager = Depends(_checkpoint),
) -> CreateJobResponse:
    try:
        cp.validate_and_lock(job_id, CheckpointType.MEDIA_SELECTION)
    except (NotAwaitingConfirmationError, WrongCheckpointError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfirmationInProgressError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    cp.release_and_resume(job_id)

    from app.tasks.qa_task import select_media_task
    select_media_task.delay(job_id)

    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.PROCESSING.value,
        message="Đang tìm media mới...",
    )


# ---------------------------------------------------------------------------
# Confirm media
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/confirm/media", response_model=CreateJobResponse)
def confirm_media(
    job_id: str,
    body: MediaConfirmRequest,
    store: JobStore = Depends(_store),
    cp: CheckpointManager = Depends(_checkpoint),
) -> CreateJobResponse:
    try:
        cp.validate_and_lock(job_id, CheckpointType.MEDIA_SELECTION)
    except (NotAwaitingConfirmationError, WrongCheckpointError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConfirmationInProgressError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    selection = MediaSelection(
        background=MinioMedia(
            bucket=body.background_bucket or settings.MINIO_BUCKET_BACKGROUNDS,
            key=body.background_key or "",
            media_type="background",
        ) if body.background_key else None,
        transitions=[
            MinioMedia(bucket=t.bucket, key=t.key, media_type=t.media_type)
            for t in body.transitions
        ],
    )

    store.update_job(job_id, media_selection=selection)
    cp.release_and_resume(job_id)

    from app.tasks.qa_task import compose_video_task
    compose_video_task.delay(job_id)

    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.PROCESSING.value,
        message="Đang tạo video...",
    )


# ---------------------------------------------------------------------------
# Download final video
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/download")
def download_video(job_id: str, store: JobStore = Depends(_store)) -> FileResponse:
    job = _get_or_404(store, job_id)
    if job.status != JobStatus.COMPLETED or not job.output_video_path:
        raise HTTPException(status_code=404, detail="Video chưa sẵn sàng")
    path = Path(job.output_video_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File không tồn tại")
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename=f"qa_video_{job_id[:8]}.mp4",
    )


# ---------------------------------------------------------------------------
# Audio preview (serve pre-generated TTS files)
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/audio/{filename}")
def get_audio_preview(
    job_id: str, filename: str, store: JobStore = Depends(_store)
) -> FileResponse:
    job = _get_or_404(store, job_id)
    audio_path = Path(job.work_dir) / "voices" / filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio không tồn tại")
    return FileResponse(path=str(audio_path), media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Random transition options (per-slot, called independently by frontend)
# ---------------------------------------------------------------------------


@router.get("/transition-options")
def get_transition_options(
    n: int = 4,
    search: str = "",
    minio: MinioClient = Depends(_minio),
) -> list[dict]:
    import random as _rnd
    if search.strip():
        gif_keys = minio.search_keys(settings.MINIO_BUCKET_GIFS, search.strip(), n * 2)
        sound_keys = minio.search_keys(settings.MINIO_BUCKET_SOUNDS, search.strip(), n * 2)
    else:
        gif_keys = minio.random_pick(settings.MINIO_BUCKET_GIFS, n)
        sound_keys = minio.random_pick(settings.MINIO_BUCKET_SOUNDS, n)
    combined = (
        [{"bucket": settings.MINIO_BUCKET_GIFS, "key": k, "media_type": "gif"} for k in gif_keys]
        + [{"bucket": settings.MINIO_BUCKET_SOUNDS, "key": k, "media_type": "sound"} for k in sound_keys]
    )
    if not search.strip():
        _rnd.shuffle(combined)
    return combined


# ---------------------------------------------------------------------------
# MinIO media proxy (to avoid CORS issues)
# ---------------------------------------------------------------------------


@router.get("/media/preview/{bucket}/{key:path}")
def preview_minio_media(
    bucket: str, key: str, minio: MinioClient = Depends(_minio)
) -> StreamingResponse:
    try:
        data = minio.stream_bytes(bucket, key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    suffix = Path(key).suffix.lower()
    content_type_map = {
        ".mp4": "video/mp4",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    content_type = content_type_map.get(suffix, "application/octet-stream")

    return StreamingResponse(iter([data]), media_type=content_type)


# ---------------------------------------------------------------------------
# Go back to previous checkpoint
# ---------------------------------------------------------------------------

CHECKPOINT_ORDER = [
    CheckpointType.TEXT_REVIEW,
    CheckpointType.VOICE_SELECTION,
    CheckpointType.MEDIA_SELECTION,
]


@router.post("/jobs/{job_id}/back", response_model=CreateJobResponse)
def go_back(
    job_id: str,
    store: JobStore = Depends(_store),
) -> CreateJobResponse:
    job = _get_or_404(store, job_id)

    # COMPLETED → back to media selection
    if job.status == JobStatus.COMPLETED:
        store.update_job(
            job_id,
            status=JobStatus.AWAITING_CONFIRMATION,
            checkpoint_type=CheckpointType.MEDIA_SELECTION,
            confirmation_lock=False,
        )
        return CreateJobResponse(
            job_id=job_id,
            status=JobStatus.AWAITING_CONFIRMATION.value,
            message="Đã quay lại chọn media",
        )

    if job.status != JobStatus.AWAITING_CONFIRMATION or job.checkpoint_type is None:
        raise HTTPException(status_code=409, detail="Job không ở trạng thái chờ xác nhận")

    current_idx = CHECKPOINT_ORDER.index(job.checkpoint_type)
    if current_idx == 0:
        raise HTTPException(status_code=400, detail="Đã ở bước đầu tiên")

    prev_checkpoint = CHECKPOINT_ORDER[current_idx - 1]
    store.update_job(
        job_id,
        checkpoint_type=prev_checkpoint,
        confirmation_lock=False,
    )

    return CreateJobResponse(
        job_id=job_id,
        status=JobStatus.AWAITING_CONFIRMATION.value,
        message="Đã quay lại bước trước",
    )


# ---------------------------------------------------------------------------
# Image preview (serve uploaded images for UI display)
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/image/{image_index}")
def get_image(
    job_id: str, image_index: int, store: JobStore = Depends(_store)
) -> FileResponse:
    job = _get_or_404(store, job_id)
    img = next((i for i in job.images if i.index == image_index), None)
    if not img:
        raise HTTPException(status_code=404, detail="Ảnh không tồn tại")
    path = Path(img.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File không tồn tại")
    suffix = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    return FileResponse(path=str(path), media_type=mime_map.get(suffix, "image/jpeg"))


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@router.delete("/jobs/{job_id}", response_model=CancelResponse)
def cancel_job(job_id: str, store: JobStore = Depends(_store)) -> CancelResponse:
    _get_or_404(store, job_id)
    store.update_job(job_id, status=JobStatus.CANCELLED)
    return CancelResponse(job_id=job_id, status=JobStatus.CANCELLED.value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_404(store: JobStore, job_id: str):
    try:
        return store.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"Job không tồn tại: {job_id}")
