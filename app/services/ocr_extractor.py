import base64
import concurrent.futures
import logging
import os
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

OCR_PROMPT = (
    "Extract all visible text from this image exactly as written, preserving line breaks. "
    "Return only the text content. If there is no text, return an empty string. "
    "Do not add any explanation or description."
)

MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class OCRExtractor:
    def __init__(self, model: str = "gpt-4o") -> None:
        self._model = model
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        return self._client

    def extract_text(self, image_path: Path) -> str:
        """Extract text from a single image using GPT-4o Vision."""
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        mime = MIME_MAP.get(image_path.suffix.lower(), "image/jpeg")

        response = self.client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{image_data}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
            max_tokens=500,
        )

        return (response.choices[0].message.content or "").strip()

    def extract_all_parallel(
        self, image_paths: list[tuple[int, Path]], max_workers: int = 5
    ) -> list[tuple[int, str]]:
        """Extract text from multiple images in parallel.

        Returns list of (image_index, text) sorted by index.
        """
        results: list[tuple[int, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.extract_text, path): idx
                for idx, path in image_paths
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    text = future.result()
                    results.append((idx, text))
                    logger.info("OCR completed for image %d: %d chars", idx, len(text))
                except Exception as exc:
                    logger.error("OCR failed for image %d: %s", idx, exc)
                    results.append((idx, ""))

        return sorted(results, key=lambda x: x[0])
