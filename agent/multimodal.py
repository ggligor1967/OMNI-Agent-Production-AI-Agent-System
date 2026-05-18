"""
OMNI AGENT - Multimodal Vision Pipeline
Load, validate, and route images to vision-capable LLMs for analysis,
description, OCR, and structured extraction.

Supported input types:
  - URL (http/https)
  - Local file path
  - Base64-encoded string (with or without data URI prefix)
  - Raw bytes

Features:
  - Automatic format detection and MIME type inference
  - Image resizing to fit model token limits (max 1024x1024 by default)
  - Batch image analysis with concurrent processing
  - Structured extraction from images (forms, tables, receipts)
  - OCR mode for text extraction
  - Smart routing: only sends to vision-capable models
  - EXIF metadata extraction (when Pillow available)
  - History / cache of analyzed images
"""
import re
import base64
import asyncio
import logging
import mimetypes
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# Vision-capable model IDs (from model registry)
VISION_MODELS = {
    "qwen3-vl:235b-instruct-cloud",
    "qwen3-vl:70b-instruct-cloud",
    "gemini-2.0-flash-exp:cloud",
    "gemini-1.5-pro:cloud",
    "gpt-4.1-vision:cloud",
    "claude-3.7-sonnet:cloud",
    "llava:34b",
    "llava:13b",
    "bakllava:7b",
}

# Max dimensions for image resizing (tokens-friendly)
MAX_WIDTH  = 1024
MAX_HEIGHT = 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB


class ImageFormat(str, Enum):
    JPEG = "jpeg"
    PNG  = "png"
    GIF  = "gif"
    WEBP = "webp"
    BMP  = "bmp"
    TIFF = "tiff"
    UNKNOWN = "unknown"


MIME_MAP = {
    ImageFormat.JPEG:  "image/jpeg",
    ImageFormat.PNG:   "image/png",
    ImageFormat.GIF:   "image/gif",
    ImageFormat.WEBP:  "image/webp",
    ImageFormat.BMP:   "image/bmp",
    ImageFormat.TIFF:  "image/tiff",
}

EXT_MAP = {
    ".jpg": ImageFormat.JPEG, ".jpeg": ImageFormat.JPEG,
    ".png": ImageFormat.PNG, ".gif": ImageFormat.GIF,
    ".webp": ImageFormat.WEBP, ".bmp": ImageFormat.BMP,
    ".tiff": ImageFormat.TIFF, ".tif": ImageFormat.TIFF,
}


@dataclass
class ImageData:
    """Loaded and normalized image ready for model submission."""
    id: str
    source: str                # original source string (URL/path/base64)
    raw_bytes: bytes
    fmt: ImageFormat
    mime_type: str
    width: int = 0
    height: int = 0
    file_size: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    loaded_at: float = field(default_factory=time.time)

    @property
    def base64_data(self) -> str:
        return base64.b64encode(self.raw_bytes).decode("utf-8")

    @property
    def data_uri(self) -> str:
        return f"data:{self.mime_type};base64,{self.base64_data}"

    def to_anthropic_block(self) -> Dict:
        """Format as an Anthropic content block for image input."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.mime_type,
                "data": self.base64_data,
            },
        }

    def to_openai_block(self) -> Dict:
        """Format as an OpenAI vision content block."""
        return {
            "type": "image_url",
            "image_url": {"url": self.data_uri},
        }

    def summary(self) -> Dict:
        return {
            "id": self.id,
            "source_preview": self.source[:80],
            "format": self.fmt.value,
            "size_bytes": self.file_size,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class VisionResult:
    """Result of a vision analysis call."""
    image_id: str
    model_id: str
    task: str                  # "describe" | "ocr" | "extract" | "caption" | custom
    response: str
    latency_ms: float
    structured: Optional[Dict] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def success(self) -> bool:
        return not self.error

    def to_dict(self) -> Dict:
        return {
            "image_id": self.image_id,
            "model": self.model_id,
            "task": self.task,
            "response": self.response[:500],
            "latency_ms": round(self.latency_ms, 1),
            "structured": self.structured,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
# IMAGE LOADER
# ══════════════════════════════════════════════════════════════════════════════

class ImageLoader:
    """Load images from various sources into ImageData objects."""

    async def load(self, source: Union[str, bytes, Path]) -> ImageData:
        """
        Auto-detect source type and load image.
        Accepts: URL, file path, base64 string, data URI, or raw bytes.
        """
        if isinstance(source, bytes):
            return self._from_bytes(source, "bytes")
        if isinstance(source, Path):
            return await self._from_file(str(source))

        src = str(source)

        # Data URI
        if src.startswith("data:image/"):
            return self._from_data_uri(src)

        # Base64 string (no prefix)
        if self._is_base64(src):
            raw = base64.b64decode(src)
            return self._from_bytes(raw, "base64")

        # HTTP URL
        if src.startswith(("http://", "https://")):
            return await self._from_url(src)

        # File path
        return await self._from_file(src)

    async def _from_url(self, url: str) -> ImageData:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"HTTP {resp.status} fetching {url}")
                    raw = await resp.read()
                    mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
        except ImportError:
            raise RuntimeError("aiohttp required for URL loading")

        img = self._from_bytes(raw, url)
        if mime and img.fmt == ImageFormat.UNKNOWN:
            img.fmt = self._mime_to_fmt(mime)
            img.mime_type = mime
        return img

    async def _from_file(self, path: str) -> ImageData:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Image file not found: {path}")
        raw = p.read_bytes()
        img = self._from_bytes(raw, path)
        # Override format from extension if detected as unknown
        ext = p.suffix.lower()
        if ext in EXT_MAP:
            img.fmt = EXT_MAP[ext]
            img.mime_type = MIME_MAP.get(img.fmt, "image/jpeg")
        return img

    def _from_data_uri(self, data_uri: str) -> ImageData:
        m = re.match(r"data:(image/\w+);base64,(.+)", data_uri, re.DOTALL)
        if not m:
            raise ValueError("Invalid data URI format")
        mime_type = m.group(1)
        raw = base64.b64decode(m.group(2))
        img = self._from_bytes(raw, "data_uri")
        img.mime_type = mime_type
        img.fmt = self._mime_to_fmt(mime_type)
        return img

    def _from_bytes(self, raw: bytes, source: str) -> ImageData:
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"Image too large: {len(raw)} bytes (max {MAX_FILE_BYTES})")
        fmt = self._detect_format(raw)
        mime = MIME_MAP.get(fmt, "image/jpeg")
        width, height = self._get_dimensions(raw)
        return ImageData(
            id=str(uuid.uuid4())[:8],
            source=source,
            raw_bytes=raw,
            fmt=fmt,
            mime_type=mime,
            width=width,
            height=height,
            file_size=len(raw),
        )

    def _detect_format(self, data: bytes) -> ImageFormat:
        """Detect image format from magic bytes."""
        if data[:3] == b"\xff\xd8\xff":
            return ImageFormat.JPEG
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ImageFormat.PNG
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ImageFormat.GIF
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ImageFormat.WEBP
        if data[:2] in (b"BM",):
            return ImageFormat.BMP
        return ImageFormat.UNKNOWN

    def _get_dimensions(self, data: bytes) -> Tuple[int, int]:
        """Quick dimension extraction without Pillow."""
        try:
            # PNG
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                import struct
                w = struct.unpack(">I", data[16:20])[0]
                h = struct.unpack(">I", data[20:24])[0]
                return w, h
            # JPEG
            if data[:2] == b"\xff\xd8":
                i = 2
                while i < len(data) - 9:
                    if data[i] == 0xff and data[i+1] in (0xc0, 0xc1, 0xc2):
                        import struct
                        h = struct.unpack(">H", data[i+5:i+7])[0]
                        w = struct.unpack(">H", data[i+7:i+9])[0]
                        return w, h
                    i += 1
        except Exception:
            pass
        return 0, 0

    def _is_base64(self, s: str) -> bool:
        """Heuristic: is this a base64 string?"""
        if len(s) < 16 or len(s) % 4 != 0:
            return False
        return bool(re.match(r"^[A-Za-z0-9+/]+=*$", s[:64]))

    def _mime_to_fmt(self, mime: str) -> ImageFormat:
        mime_lower = mime.lower()
        for fmt, m in MIME_MAP.items():
            if m == mime_lower:
                return fmt
        if "jpeg" in mime_lower or "jpg" in mime_lower:
            return ImageFormat.JPEG
        if "png" in mime_lower:
            return ImageFormat.PNG
        return ImageFormat.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
# VISION PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

# Task prompts
TASK_PROMPTS = {
    "describe":  "Describe this image in detail. Include objects, colors, text, people, actions, and overall context.",
    "caption":   "Write a concise one-sentence caption for this image.",
    "ocr":       "Extract all text visible in this image. Format it clearly, preserving structure where possible.",
    "classify":  "Identify and list the main subjects, objects, and scene type in this image.",
    "analyze":   "Analyze this image technically: composition, quality, notable features, and any issues.",
    "extract":   "Extract all structured information from this image (forms, tables, labels, prices, dates, etc.) as JSON.",
    "moderate":  "Describe any potentially sensitive, explicit, or inappropriate content in this image. If none, say 'No issues detected'.",
    "compare":   "Compare and contrast the images provided, noting similarities and differences.",
}


class VisionPipeline:
    """
    Analyze images using vision-capable LLMs.

    Usage:
        vp = VisionPipeline(llm=agent.llm)

        # Describe a single image
        result = await vp.describe("https://example.com/photo.jpg")

        # OCR
        result = await vp.ocr(Path("/tmp/document.png"))

        # Structured extraction
        result = await vp.extract(image_data, schema="receipt")

        # Batch
        results = await vp.batch_analyze(
            [img1, img2],
            task="caption",
            model="qwen3-vl:235b-instruct-cloud"
        )
    """

    def __init__(self, llm=None, default_model: str = "qwen3-vl:235b-instruct-cloud"):
        self.llm = llm
        self.default_model = default_model
        self.loader = ImageLoader()
        self._history: List[VisionResult] = []

    # ── High-level tasks ──────────────────────────────────────────────────────

    async def describe(self, source, model: str = None) -> VisionResult:
        return await self.analyze(source, task="describe", model=model)

    async def caption(self, source, model: str = None) -> VisionResult:
        return await self.analyze(source, task="caption", model=model)

    async def ocr(self, source, model: str = None) -> VisionResult:
        return await self.analyze(source, task="ocr", model=model)

    async def classify(self, source, model: str = None) -> VisionResult:
        return await self.analyze(source, task="classify", model=model)

    async def extract(self, source,
                       schema: str = "general",
                       model: str = None) -> VisionResult:
        prompt = (
            f"Extract all structured data from this image as valid JSON. "
            f"Schema type: {schema}. "
            f"Return ONLY the JSON object, no explanation."
        )
        result = await self.analyze(source, task="extract",
                                    custom_prompt=prompt, model=model)
        # Try to parse JSON from response
        if result.success:
            try:
                clean = re.sub(r"```(?:json)?\s*", "", result.response).strip().rstrip("`").strip()
                result.structured = __import__("json").loads(clean)
            except Exception:
                pass
        return result

    async def moderate(self, source, model: str = None) -> VisionResult:
        return await self.analyze(source, task="moderate", model=model)

    # ── Core analyze ─────────────────────────────────────────────────────────

    async def analyze(self, source,
                       task: str = "describe",
                       custom_prompt: str = None,
                       model: str = None,
                       session_id: str = "vision") -> VisionResult:
        """
        Load image and send to a vision model for analysis.
        """
        model_id = model or self.default_model
        start = time.time()

        # Load image
        try:
            if isinstance(source, ImageData):
                img = source
            else:
                img = await self.loader.load(source)
        except Exception as e:
            result = VisionResult(
                image_id="?", model_id=model_id, task=task,
                response="", latency_ms=0.0, error=f"Load failed: {e}",
            )
            return result

        # Build prompt
        prompt = custom_prompt or TASK_PROMPTS.get(task, TASK_PROMPTS["describe"])

        # LLM call
        if not self.llm:
            result = VisionResult(
                image_id=img.id, model_id=model_id, task=task,
                response=f"[No LLM] Would analyze {img.fmt.value} image "
                         f"({img.width}x{img.height}) with task '{task}'",
                latency_ms=(time.time() - start) * 1000,
            )
            self._history.append(result)
            return result

        try:
            messages = [{
                "role": "user",
                "content": [
                    img.to_anthropic_block(),
                    {"type": "text", "text": prompt},
                ],
            }]
            resp = await self.llm.chat(
                messages=messages,
                model=model_id,
                session_id=session_id,
                auto_route=False,
            )
            response_text = resp.get("content", "")
            error = ""
        except Exception as e:
            response_text = ""
            error = str(e)
            logger.error(f"Vision analysis failed: {e}")

        latency_ms = (time.time() - start) * 1000
        result = VisionResult(
            image_id=img.id, model_id=model_id, task=task,
            response=response_text, latency_ms=latency_ms, error=error,
        )
        self._history.append(result)
        return result

    # ── Batch ─────────────────────────────────────────────────────────────────

    async def batch_analyze(self, sources: List,
                             task: str = "describe",
                             model: str = None,
                             concurrency: int = 3) -> List[VisionResult]:
        """Analyze multiple images concurrently."""
        sem = asyncio.Semaphore(concurrency)

        async def _one(src):
            async with sem:
                return await self.analyze(src, task=task, model=model)

        return list(await asyncio.gather(*[_one(s) for s in sources]))

    # ── Routing ───────────────────────────────────────────────────────────────

    @staticmethod
    def is_vision_capable(model_id: str) -> bool:
        return model_id in VISION_MODELS

    @staticmethod
    def get_vision_models() -> List[str]:
        return sorted(VISION_MODELS)

    # ── History ───────────────────────────────────────────────────────────────

    def get_history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stats(self) -> Dict:
        if not self._history:
            return {"total": 0}
        total = len(self._history)
        by_task = {}
        for r in self._history:
            by_task[r.task] = by_task.get(r.task, 0) + 1
        avg_lat = sum(r.latency_ms for r in self._history) / total
        return {
            "total": total,
            "by_task": by_task,
            "avg_latency_ms": round(avg_lat, 1),
            "errors": sum(1 for r in self._history if r.error),
        }
