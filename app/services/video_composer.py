"""Video composer: vertical (9:16) video with full-screen MP4 background.

Layout (1080x1920):
  Background fills full screen.
  Q/A images centered on top of background.
  GIF/sound transitions between segments (gif overlaid full-screen, sound plays over bg).

Audio mix:
  - TTS / sound effects embedded in each clip
  - After concat: background MP4 audio looped at low volume and mixed in
"""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

W = 1080
H = 1920
FPS = 24
IMG_MAX_W = 920   # max width for Q/A image overlay
IMG_MAX_H = 560   # max height for Q/A image overlay
IMG_TOP_PAD = 80  # pixels from top edge


@dataclass
class QASegment:
    image_path: Path
    audio_path: Path
    duration: float


@dataclass
class TransitionConfig:
    duration: float = 2.0
    gif_path: Path | None = None   # visual (gif/mp4) — mutually exclusive with sound_path
    sound_path: Path | None = None  # audio-only transition


class VideoComposerError(Exception):
    pass


class VideoComposer:
    def __init__(
        self,
        transition_duration: float = 2.0,
        bg_audio_volume: float = 0.3,
    ) -> None:
        self._trans_dur = transition_duration
        self._bg_vol = bg_audio_volume

    def compose(
        self,
        segments: list[QASegment],
        transitions: list[TransitionConfig],  # len == len(segments) - 1
        background_video_path: Path | None,
        output_path: Path,
        work_dir: Path,
    ) -> Path:
        if not segments:
            raise VideoComposerError("No segments to compose")

        clips_dir = work_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        clip_paths: list[Path] = []
        for i, seg in enumerate(segments):
            clip = clips_dir / f"qa_{i:03d}.mp4"
            self._make_qa_clip(seg, clip, background_video_path)
            clip_paths.append(clip)

            if i < len(segments) - 1:
                t = transitions[i] if i < len(transitions) else TransitionConfig(duration=self._trans_dur)
                trans = clips_dir / f"trans_{i:03d}.mp4"
                self._make_transition_clip(t, trans, background_video_path)
                clip_paths.append(trans)

        concat_path = work_dir / "concat.mp4"
        self._concat(clip_paths, concat_path)

        concat_path.rename(output_path)

        return output_path

    # ------------------------------------------------------------------
    # Q/A clip: full-screen bg, image centered on top
    # ------------------------------------------------------------------

    def _make_qa_clip(self, seg: QASegment, out: Path, bg: Path | None) -> None:
        duration = max(seg.duration, 0.5)

        # Image: scale to fit within full width, capped at IMG_MAX_H, positioned near top
        img_filter = (
            f"scale={IMG_MAX_W}:{IMG_MAX_H}:force_original_aspect_ratio=decrease,setsar=1"
        )
        img_overlay = f"overlay=(main_w-overlay_w)/2:{IMG_TOP_PAD}"

        if bg:
            filter_complex = (
                f"[0:v]scale=-2:{H},crop={W}:{H},setsar=1,fps={FPS}[bg];"
                f"[1:v]{img_filter}[img];"
                f"[bg][img]{img_overlay}[v];"
                f"[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", str(bg),
                "-loop", "1", "-i", str(seg.image_path),
                "-i", str(seg.audio_path),
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                "-t", str(duration),
                *self._encode_args(),
                str(out),
            ]
        else:
            filter_complex = (
                f"color=black:size={W}x{H}:rate={FPS}[bg];"
                f"[0:v]{img_filter}[img];"
                f"[bg][img]{img_overlay}[v];"
                f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(seg.image_path),
                "-i", str(seg.audio_path),
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                "-t", str(duration),
                *self._encode_args(),
                str(out),
            ]

        self._run(cmd, f"qa_clip {out.name}")

    # ------------------------------------------------------------------
    # Transition clip: either gif overlay or sound over bg
    # ------------------------------------------------------------------

    def _make_transition_clip(
        self, transition: TransitionConfig, out: Path, bg: Path | None
    ) -> None:
        duration = transition.duration
        inputs: list[str] = []
        filter_parts: list[str] = []
        next_idx = 0

        # Background layer
        if bg:
            inputs += ["-stream_loop", "-1", "-i", str(bg)]
            filter_parts.append(
                f"[{next_idx}:v]scale=-2:{H},crop={W}:{H},setsar=1,fps={FPS}[vbg]"
            )
            next_idx += 1
        else:
            filter_parts.append(f"color=black:size={W}x{H}:rate={FPS}[vbg]")

        # Visual: gif overlay (centered)
        if transition.gif_path:
            inputs += ["-stream_loop", "-1", "-i", str(transition.gif_path)]
            filter_parts.append(
                f"[{next_idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"setsar=1,fps={FPS}[gif]"
            )
            filter_parts.append("[vbg][gif]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[v]")
            next_idx += 1
        else:
            filter_parts.append("[vbg]null[v]")

        # Audio: sound effect (resampled) or generated silence
        if transition.sound_path:
            inputs += ["-i", str(transition.sound_path)]
            filter_parts.append(
                f"[{next_idx}:a]aresample=44100,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
            )
        else:
            filter_parts.append(
                "anullsrc=channel_layout=stereo:sample_rate=44100[a]"
            )

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[v]", "-map", "[a]",
            "-t", str(duration),
            *self._encode_args(),
            str(out),
        ]
        self._run(cmd, f"transition {out.name}")

    # ------------------------------------------------------------------
    # Concat + audio mix
    # ------------------------------------------------------------------

    def _concat(self, clips: list[Path], out: Path) -> None:
        list_file = out.parent / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{p.absolute()}'" for p in clips),
            encoding="utf-8",
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100",
            str(out),
        ]
        self._run(cmd, "concat")

    def _extract_audio(self, video: Path, out: Path) -> None:
        cmd = [
            "ffmpeg", "-y", "-i", str(video),
            "-vn", "-acodec", "aac", "-ar", "44100",
            str(out),
        ]
        self._run(cmd, "extract_bg_audio")

    def _mix_bg_audio(self, video: Path, bg_audio: Path, out: Path) -> None:
        vol = self._bg_vol
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-stream_loop", "-1", "-i", str(bg_audio),
            "-filter_complex",
            f"[1:a]volume={vol}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac",
            str(out),
        ]
        self._run(cmd, "mix_bg_audio")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_audio(video: Path) -> bool:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(result.stdout.strip())

    @staticmethod
    def _encode_args() -> list[str]:
        return [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-r", str(FPS),
        ]

    @staticmethod
    def _run(cmd: list[str], label: str) -> None:
        logger.debug("FFmpeg [%s]: %s", label, " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise VideoComposerError(
                f"FFmpeg [{label}] failed:\n{result.stderr[-800:]}"
            )
