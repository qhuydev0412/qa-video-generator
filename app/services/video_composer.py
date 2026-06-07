"""Video composer: vertical (9:16) video with MP4 background.

Layout (1080x1920):
  Top half  (1080×960): Q/A image overlaid on background
  Bottom half (1080×960): background shows through; GIF overlaid during transitions

Audio mix:
  - TTS / sound effects embedded in each clip
  - After concat: background MP4 audio looped at low volume and mixed in
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

W = 1080
H = 1920
HALF = H // 2  # 960
FPS = 24


@dataclass
class QASegment:
    image_path: Path
    audio_path: Path
    duration: float


@dataclass
class TransitionConfig:
    gif_path: Path | None
    sound_path: Path | None
    duration: float = 2.0


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
        transition: TransitionConfig,
        background_video_path: Path | None,
        output_path: Path,
        work_dir: Path,
    ) -> Path:
        if not segments:
            raise VideoComposerError("No segments to compose")

        clips_dir = work_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Build clips (each has TTS / sfx audio already)
        clip_paths: list[Path] = []
        for i, seg in enumerate(segments):
            clip = clips_dir / f"qa_{i:03d}.mp4"
            self._make_qa_clip(seg, clip, background_video_path)
            clip_paths.append(clip)

            if i < len(segments) - 1:
                trans = clips_dir / f"trans_{i:03d}.mp4"
                self._make_transition_clip(
                    seg.image_path, transition, trans, background_video_path
                )
                clip_paths.append(trans)

        # Concatenate all clips
        concat_path = work_dir / "concat.mp4"
        self._concat(clip_paths, concat_path)

        # Extract & mix background audio from MP4 (if available)
        if background_video_path and self._has_audio(background_video_path):
            bg_audio = work_dir / "bg_audio.aac"
            self._extract_audio(background_video_path, bg_audio)
            self._mix_bg_audio(concat_path, bg_audio, output_path)
        else:
            concat_path.rename(output_path)

        return output_path

    # ------------------------------------------------------------------
    # Clip builders
    # ------------------------------------------------------------------

    def _make_qa_clip(
        self, seg: QASegment, out: Path, bg: Path | None
    ) -> None:
        """Image overlaid on background (top half) + TTS audio."""
        duration = max(seg.duration, 0.5)

        if bg:
            filter_complex = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps={FPS}[bg];"
                f"[1:v]scale={W}:{HALF}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{HALF}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[img];"
                f"[bg][img]overlay=0:0[v]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", str(bg),
                "-loop", "1", "-i", str(seg.image_path),
                "-i", str(seg.audio_path),
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "2:a",
                "-t", str(duration),
                *self._encode_args(),
                str(out),
            ]
        else:
            filter_complex = (
                f"[0:v]scale={W}:{HALF}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{HALF}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[top];"
                f"color=black:size={W}x{HALF}:rate={FPS}[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(seg.image_path),
                "-i", str(seg.audio_path),
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "1:a",
                "-t", str(duration),
                *self._encode_args(),
                str(out),
            ]

        self._run(cmd, f"qa_clip {out.name}")

    def _make_transition_clip(
        self,
        last_image: Path,
        transition: TransitionConfig,
        out: Path,
        bg: Path | None,
    ) -> None:
        """Last image on top, GIF in bottom half; transition sound."""
        duration = transition.duration

        if bg:
            inputs = ["-stream_loop", "-1", "-i", str(bg)]
            # image input index = 1
            inputs += ["-loop", "1", "-i", str(last_image)]
            next_idx = 2

            parts = [
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps={FPS}[bg]",
                f"[1:v]scale={W}:{HALF}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{HALF}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[img]",
                f"[bg][img]overlay=0:0[v0]",
            ]

            if transition.gif_path:
                inputs += ["-stream_loop", "-1", "-i", str(transition.gif_path)]
                gif_idx = next_idx
                next_idx += 1
                parts += [
                    f"[{gif_idx}:v]scale={W}:{HALF}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{HALF},setsar=1,fps={FPS}[gif]",
                    f"[v0][gif]overlay=0:{HALF}[v]",
                ]
            else:
                parts.append("[v0]null[v]")

        else:
            inputs = ["-loop", "1", "-i", str(last_image)]
            next_idx = 1

            parts = [
                f"[0:v]scale={W}:{HALF}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{HALF}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[top]",
            ]
            if transition.gif_path:
                inputs += ["-stream_loop", "-1", "-i", str(transition.gif_path)]
                gif_idx = next_idx
                next_idx += 1
                parts += [
                    f"[{gif_idx}:v]scale={W}:{HALF}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{HALF},setsar=1,fps={FPS}[gif]",
                    f"[top][gif]vstack=inputs=2[v]",
                ]
            else:
                parts += [
                    f"color=black:size={W}x{HALF}:rate={FPS}[bottom]",
                    "[top][bottom]vstack=inputs=2[v]",
                ]

        filter_complex = ";".join(parts)

        # Audio: sound effect or silence
        if transition.sound_path:
            inputs += ["-i", str(transition.sound_path)]
            audio_map = ["-map", f"{next_idx}:a"]
        else:
            inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            audio_map = ["-map", f"{next_idx}:a"]

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            *audio_map,
            "-t", str(duration),
            *self._encode_args(),
            str(out),
        ]
        self._run(cmd, f"transition {out.name}")

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
            "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
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
