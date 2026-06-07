"""Celery tasks for the QA video generation pipeline."""

import concurrent.futures
import logging
from pathlib import Path

from celery import shared_task

from app.core.config import settings
from app.models.job import (
    CheckpointType,
    ExtractedText,
    JobStatus,
    MediaOptions,
    MinioMedia,
    PipelineStep,
    SegmentVoiceOptions,
    VoicePreview,
)
from app.services.checkpoint_manager import CheckpointManager
from app.services.job_store import JobStore
from app.services.media_selector import MediaSelector
from app.services.minio_client import MinioClient
from app.services.ocr_extractor import OCRExtractor
from app.services.voice_synthesizer import VoiceSynthesizer

logger = logging.getLogger(__name__)


def _get_store() -> JobStore:
    return JobStore()


def _get_checkpoint(store: JobStore) -> CheckpointManager:
    return CheckpointManager(store)


# ---------------------------------------------------------------------------
# Task: Extract text from all images in parallel
# ---------------------------------------------------------------------------


@shared_task(bind=True, name="qa.extract_texts")
def extract_texts_task(self, job_id: str) -> None:
    store = _get_store()
    checkpoint = _get_checkpoint(store)

    store.update_job(
        job_id,
        status=JobStatus.PROCESSING,
        current_step=PipelineStep.EXTRACTING_TEXT,
        progress_percent=5,
    )

    try:
        job = store.get_job(job_id)
        extractor = OCRExtractor(model=settings.OCR_MODEL)

        image_paths = [
            (img.index, Path(img.path))
            for img in job.images
        ]

        results = extractor.extract_all_parallel(image_paths)
        store.update_job(job_id, progress_percent=20)

        extracted = [
            ExtractedText(image_index=idx, text=text)
            for idx, text in results
        ]

        store.update_job(job_id, extracted_texts=extracted, progress_percent=25)
        checkpoint.pause_at_checkpoint(job_id, CheckpointType.TEXT_REVIEW)

    except Exception as exc:
        logger.exception("extract_texts_task failed for job %s", job_id)
        store.update_job(job_id, status=JobStatus.FAILED)
        raise


# ---------------------------------------------------------------------------
# Task: Generate all 6 TTS voices for every segment
# ---------------------------------------------------------------------------


_DEFAULT_VOICE_ID = "alloy"
_DEFAULT_VOICE_NAME = "Alloy (Trung tính)"


@shared_task(bind=True, name="qa.generate_voices")
def generate_voices_task(self, job_id: str) -> None:
    store = _get_store()
    checkpoint = _get_checkpoint(store)

    store.update_job(
        job_id,
        status=JobStatus.PROCESSING,
        current_step=PipelineStep.GENERATING_VOICES,
        progress_percent=30,
    )

    try:
        job = store.get_job(job_id)
        synthesizer = VoiceSynthesizer(model=settings.TTS_MODEL)

        voices_dir = Path(job.work_dir) / "voices"
        voices_dir.mkdir(parents=True, exist_ok=True)

        texts = {t.image_index: t.text for t in job.extracted_texts}

        # Generate default voice for all segments in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures: dict = {}
            for img in job.images:
                text = texts.get(img.index, "")
                if not text.strip():
                    continue
                out = voices_dir / f"img{img.index}_{_DEFAULT_VOICE_ID}.mp3"
                future = executor.submit(synthesizer.synthesize, text, _DEFAULT_VOICE_ID, out)
                futures[future] = (img.index, text, out)

            segment_results: dict[int, tuple[str, Path]] = {}
            for future in concurrent.futures.as_completed(futures):
                idx, text, path = futures[future]
                try:
                    future.result()
                    segment_results[idx] = (text, path)
                except Exception as exc:
                    logger.error("Voice gen failed for image %d: %s", idx, exc)

        voice_options_per_segment: list[SegmentVoiceOptions] = []
        for img in sorted(job.images, key=lambda x: x.index):
            if img.index not in segment_results:
                continue
            text, audio_path = segment_results[img.index]
            preview = VoicePreview(
                voice_id=_DEFAULT_VOICE_ID,
                voice_name=_DEFAULT_VOICE_NAME,
                audio_path=str(audio_path),
            )
            voice_options_per_segment.append(
                SegmentVoiceOptions(
                    image_index=img.index,
                    text=text,
                    options=[preview],
                    selected_voice_id=_DEFAULT_VOICE_ID,
                    selected_audio_path=str(audio_path),
                )
            )

        store.update_job(
            job_id,
            voice_options_per_segment=voice_options_per_segment,
            progress_percent=50,
        )
        checkpoint.pause_at_checkpoint(job_id, CheckpointType.VOICE_SELECTION)

    except Exception as exc:
        logger.exception("generate_voices_task failed for job %s", job_id)
        store.update_job(job_id, status=JobStatus.FAILED)
        raise


# ---------------------------------------------------------------------------
# Task: Pick 3 random assets from each MinIO bucket
# ---------------------------------------------------------------------------


@shared_task(bind=True, name="qa.select_media")
def select_media_task(self, job_id: str) -> None:
    store = _get_store()
    checkpoint = _get_checkpoint(store)

    store.update_job(
        job_id,
        status=JobStatus.PROCESSING,
        current_step=PipelineStep.SELECTING_MEDIA,
        progress_percent=55,
    )

    try:
        minio = MinioClient(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        selector = MediaSelector(minio)

        options = selector.pick_options(
            backgrounds_bucket=settings.MINIO_BUCKET_BACKGROUNDS,
            gifs_bucket=settings.MINIO_BUCKET_GIFS,
            sounds_bucket=settings.MINIO_BUCKET_SOUNDS,
            n=3,
        )

        store.update_job(job_id, media_options=options, progress_percent=65)
        checkpoint.pause_at_checkpoint(job_id, CheckpointType.MEDIA_SELECTION)

    except Exception as exc:
        logger.exception("select_media_task failed for job %s", job_id)
        store.update_job(job_id, status=JobStatus.FAILED)
        raise


# ---------------------------------------------------------------------------
# Task: Compose final video
# ---------------------------------------------------------------------------


@shared_task(bind=True, name="qa.compose_video")
def compose_video_task(self, job_id: str) -> None:
    store = _get_store()

    store.update_job(
        job_id,
        status=JobStatus.PROCESSING,
        current_step=PipelineStep.COMPOSING_VIDEO,
        progress_percent=70,
    )

    try:
        job = store.get_job(job_id)
        work_dir = Path(job.work_dir)

        # Collect selected voice audio paths in image order
        voice_map = {
            seg.image_index: seg.selected_audio_path
            for seg in job.voice_options_per_segment
            if seg.selected_audio_path
        }

        from app.services.video_composer import QASegment, TransitionConfig, VideoComposer

        segments: list[QASegment] = []
        for img in sorted(job.images, key=lambda x: x.index):
            audio_path_str = voice_map.get(img.index)
            if not audio_path_str:
                logger.warning("No audio for image %d, skipping", img.index)
                continue
            audio_path = Path(audio_path_str)
            import json, subprocess as sp
            try:
                r = sp.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)], capture_output=True, text=True, timeout=30)
                duration = float(json.loads(r.stdout)["format"]["duration"]) if r.returncode == 0 else 3.0
            except Exception:
                duration = 3.0
            segments.append(
                QASegment(
                    image_path=Path(img.path),
                    audio_path=audio_path,
                    duration=duration,
                )
            )

        # Download selected media from MinIO
        minio = MinioClient(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )

        media = job.media_selection
        gif_path: Path | None = None
        sound_path: Path | None = None
        bg_video_path: Path | None = None

        if media:
            if media.gif and media.gif.key:
                gif_path = work_dir / "media" / f"meme{Path(media.gif.key).suffix}"
                minio.download(media.gif.bucket, media.gif.key, gif_path)

            if media.sound and media.sound.key:
                sound_path = work_dir / "media" / f"sound{Path(media.sound.key).suffix}"
                minio.download(media.sound.bucket, media.sound.key, sound_path)

            if media.background and media.background.key:
                bg_ext = Path(media.background.key).suffix
                bg_video_path = work_dir / "media" / f"background{bg_ext}"
                minio.download(media.background.bucket, media.background.key, bg_video_path)

        store.update_job(job_id, progress_percent=75)

        transition = TransitionConfig(
            gif_path=gif_path,
            sound_path=sound_path,
            duration=settings.TRANSITION_DURATION,
        )

        output_dir = work_dir / "output"
        output_path = output_dir / "final.mp4"

        composer = VideoComposer(
            transition_duration=settings.TRANSITION_DURATION,
            bg_audio_volume=settings.BACKGROUND_AUDIO_VOLUME,
        )
        composer.compose(
            segments=segments,
            transition=transition,
            background_video_path=bg_video_path,
            output_path=output_path,
            work_dir=work_dir,
        )

        store.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            output_video_path=str(output_path),
            progress_percent=100,
        )
        logger.info("Job %s completed: %s", job_id, output_path)

    except Exception as exc:
        logger.exception("compose_video_task failed for job %s", job_id)
        store.update_job(job_id, status=JobStatus.FAILED)
        raise
