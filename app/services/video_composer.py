"""Video composer: vertical (9:16) video with full-screen MP4 background.

Single-pass FFmpeg: background runs continuously, images overlay with time-gated
enable expressions, transition gifs stack on top. Audio streams are delayed to their
absolute timeline position then mixed.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

W = 1080
H = 1920
FPS = 24
IMG_MAX_W = 1020
IMG_MAX_H = 500
AUDIO_VOLUME = 2.0   # boost factor for all audio streams (TTS + transition)

_IMG_CENTER_Y = 640                                    # must match overlay expression
READING_GIF_TOP = _IMG_CENTER_Y + IMG_MAX_H // 2 + 50  # just below max image extent = 940
READING_GIF_H = H - READING_GIF_TOP                    # = 1000


@dataclass
class QASegment:
    image_path: Path
    audio_path: Path
    duration: float
    reading_gif_path: Path | None = None  # gif shown full-screen behind the QA image during reading


@dataclass
class TransitionConfig:
    duration: float = 2.0
    gif_path: Path | None = None
    sound_path: Path | None = None


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
        transitions: list[TransitionConfig],
        background_video_path: Path | None,
        output_path: Path,
        work_dir: Path,
    ) -> Path:
        if not segments:
            raise VideoComposerError("No segments to compose")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Timeline ──────────────────────────────────────────────────────────
        seg_starts: list[float] = []
        trans_starts: list[float] = []
        t = 0.0
        for i, seg in enumerate(segments):
            seg_starts.append(t)
            t += seg.duration
            if i < len(transitions):
                trans_starts.append(t)
                t += transitions[i].duration
        total_dur = t

        # Each image is visible from its segment start until the NEXT segment
        # starts (i.e., it stays on screen through the following transition).
        img_vis_end = [
            seg_starts[i + 1] if i + 1 < len(segments) else total_dur
            for i in range(len(segments))
        ]

        # ── Inputs ────────────────────────────────────────────────────────────
        input_args: list[str] = []
        next_idx = 0

        # Background
        bg_vidx: int | None = None
        if background_video_path:
            input_args += ["-stream_loop", "-1", "-i", str(background_video_path)]
            bg_vidx = next_idx
            next_idx += 1

        # Reading gifs (one per segment, looped)
        reading_gif_vidx: list[int | None] = []
        for seg in segments:
            if seg.reading_gif_path:
                input_args += ["-stream_loop", "-1", "-i", str(seg.reading_gif_path)]
                reading_gif_vidx.append(next_idx)
                next_idx += 1
            else:
                reading_gif_vidx.append(None)

        # Images (looped stills)
        img_vidx: list[int] = []
        for seg in segments:
            input_args += ["-loop", "1", "-i", str(seg.image_path)]
            img_vidx.append(next_idx)
            next_idx += 1

        # Transition gifs
        gif_vidx: list[int | None] = []
        for trans in transitions:
            if trans.gif_path:
                input_args += ["-stream_loop", "-1", "-i", str(trans.gif_path)]
                gif_vidx.append(next_idx)
                next_idx += 1
            else:
                gif_vidx.append(None)

        # Transition sounds
        sound_aidx: list[int | None] = []
        for trans in transitions:
            if trans.sound_path:
                input_args += ["-i", str(trans.sound_path)]
                sound_aidx.append(next_idx)
                next_idx += 1
            else:
                sound_aidx.append(None)

        # TTS audio (separate inputs from the images)
        tts_aidx: list[int] = []
        for seg in segments:
            input_args += ["-i", str(seg.audio_path)]
            tts_aidx.append(next_idx)
            next_idx += 1

        # ── filter_complex ────────────────────────────────────────────────────
        fp: list[str] = []

        # Background video: fill height, crop width, run continuously
        if bg_vidx is not None:
            fp.append(
                f"[{bg_vidx}:v]scale=-2:{H},crop={W}:{H},setsar=1,fps={FPS}[vbg]"
            )
        else:
            fp.append(
                f"color=black:size={W}x{H}:rate={FPS}:duration={total_dur}[vbg]"
            )

        # Overlay reading gifs full-screen during each segment (behind QA image)
        prev_v = "vbg"
        for i in range(len(segments)):
            if reading_gif_vidx[i] is not None:
                rgin = f"rgifraw{i}"
                rgout = f"vrgif{i}"
                vs = seg_starts[i]
                ve = seg_starts[i] + segments[i].duration
                fp.append(
                    f"[{reading_gif_vidx[i]}:v]scale=-2:{READING_GIF_H}"
                    f",crop={W}:{READING_GIF_H},setsar=1,fps={FPS}[{rgin}]"
                )
                fp.append(
                    f"[{prev_v}][{rgin}]overlay=0:{READING_GIF_TOP}"
                    f":enable='between(t,{vs},{ve})'[{rgout}]"
                )
                prev_v = rgout

        # Overlay each QA image for [seg_start, img_vis_end] (stays through transition)
        img_scale = (
            f"scale={IMG_MAX_W}:{IMG_MAX_H}"
            f":force_original_aspect_ratio=decrease,setsar=1"
        )
        for i in range(len(segments)):
            vin = f"imgraw{i}"
            vout = f"vimgd{i}"
            vs = seg_starts[i]
            ve = img_vis_end[i]
            fp.append(f"[{img_vidx[i]}:v]{img_scale}[{vin}]")
            fp.append(
                f"[{prev_v}][{vin}]overlay=(main_w-overlay_w)/2:640-overlay_h/2"
                f":enable='between(t,{vs},{ve})'[{vout}]"
            )
            prev_v = vout

        # Overlay transition gifs on top of the image layer
        for i, trans in enumerate(transitions):
            if gif_vidx[i] is not None:
                gin = f"gifraw{i}"
                gout = f"vgifd{i}"
                ts = trans_starts[i]
                te = ts + trans.duration
                fp.append(
                    f"[{gif_vidx[i]}:v]scale={W}:{H}"
                    f":force_original_aspect_ratio=decrease,setsar=1,fps={FPS}[{gin}]"
                )
                fp.append(
                    f"[{prev_v}][{gin}]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2"
                    f":enable='between(t,{ts},{te})'[{gout}]"
                )
                prev_v = gout

        fp.append(f"[{prev_v}]setpts=PTS-STARTPTS[vout]")

        # Audio: delay each stream to its absolute timeline position then mix
        a_labels: list[str] = []

        for i in range(len(segments)):
            delay_ms = int(seg_starts[i] * 1000)
            al = f"atts{i}"
            fp.append(
                f"[{tts_aidx[i]}:a]aresample=44100"
                f",aformat=sample_fmts=fltp:channel_layouts=stereo"
                f",volume={AUDIO_VOLUME}"
                f",adelay={delay_ms}|{delay_ms}[{al}]"
            )
            a_labels.append(f"[{al}]")

        for i, trans in enumerate(transitions):
            delay_ms = int(trans_starts[i] * 1000)
            trans_dur = trans.duration
            if sound_aidx[i] is not None:
                al = f"asound{i}"
                fp.append(
                    f"[{sound_aidx[i]}:a]aresample=44100"
                    f",aformat=sample_fmts=fltp:channel_layouts=stereo"
                    f",volume={AUDIO_VOLUME}"
                    f",atrim=duration={trans_dur}"
                    f",adelay={delay_ms}|{delay_ms}[{al}]"
                )
                a_labels.append(f"[{al}]")
            elif gif_vidx[i] is not None and self._has_audio(trans.gif_path):
                al = f"agif{i}"
                fp.append(
                    f"[{gif_vidx[i]}:a]aresample=44100"
                    f",aformat=sample_fmts=fltp:channel_layouts=stereo"
                    f",volume={AUDIO_VOLUME}"
                    f",atrim=duration={trans_dur}"
                    f",adelay={delay_ms}|{delay_ms}[{al}]"
                )
                a_labels.append(f"[{al}]")

        if len(a_labels) > 1:
            fp.append(
                f"{''.join(a_labels)}"
                f"amix=inputs={len(a_labels)}:duration=longest"
                f":dropout_transition=0:normalize=0[aout]"
            )
        elif len(a_labels) == 1:
            fp.append(f"{a_labels[0]}acopy[aout]")
        else:
            fp.append("anullsrc=channel_layout=stereo:sample_rate=44100[aout]")

        # ── Encode ────────────────────────────────────────────────────────────
        cmd = [
            "ffmpeg", "-y",
            *input_args,
            "-filter_complex", ";".join(fp),
            "-map", "[vout]", "-map", "[aout]",
            "-t", str(total_dur),
            *self._encode_args(),
            str(output_path),
        ]
        self._run(cmd, "compose_all")
        return output_path

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _has_audio(video: Path) -> bool:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                str(video),
            ],
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
                f"FFmpeg [{label}] failed:\n{result.stderr[-1200:]}"
            )
