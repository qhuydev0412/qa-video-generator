import concurrent.futures
import json
import logging
import os
import subprocess
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

VOICES = [
    {"id": "alloy", "name": "Alloy (Trung tính)"},
    {"id": "echo", "name": "Echo (Nam, trầm)"},
    {"id": "fable", "name": "Fable (Biểu cảm)"},
    {"id": "onyx", "name": "Onyx (Nam, uy)"},
    {"id": "nova", "name": "Nova (Nữ, ấm)"},
    {"id": "shimmer", "name": "Shimmer (Nữ, nhẹ)"},
]


class VoiceSynthesizer:
    def __init__(self, model: str = "tts-1") -> None:
        self._model = model
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        return self._client

    def synthesize(self, text: str, voice_id: str, output_path: Path, speed: float = 1.2) -> float:
        """Generate TTS audio. Returns duration in seconds."""
        response = self.client.audio.speech.create(
            model=self._model,
            voice=voice_id,  # type: ignore[arg-type]
            input=text,
            response_format="mp3",
            speed=speed,
        )
        response.stream_to_file(str(output_path))
        return self._get_duration(output_path)

    def synthesize_all_voices(
        self, text: str, output_dir: Path, prefix: str
    ) -> list[dict]:
        """Generate TTS for all 6 voices in parallel.

        Returns list of {voice_id, voice_name, audio_path, duration}.
        """
        tasks = [
            {
                "voice_id": v["id"],
                "voice_name": v["name"],
                "path": output_dir / f"{prefix}_{v['id']}.mp3",
            }
            for v in VOICES
        ]

        results: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            future_to_task = {
                executor.submit(self.synthesize, text, t["voice_id"], t["path"]): t
                for t in tasks
            }
            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    duration = future.result()
                    results.append(
                        {
                            "voice_id": task["voice_id"],
                            "voice_name": task["voice_name"],
                            "audio_path": str(task["path"]),
                            "duration": duration,
                        }
                    )
                except Exception as exc:
                    logger.error("TTS failed for voice %s: %s", task["voice_id"], exc)

        return sorted(results, key=lambda x: x["voice_id"])

    @staticmethod
    def _get_duration(path: Path) -> float:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return float(json.loads(result.stdout)["format"]["duration"])
        except Exception:
            pass
        return 0.0
