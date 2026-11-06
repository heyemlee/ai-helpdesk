"""Validate bytes before decoding; originals stay private, previews remove metadata."""
from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 8192
MAX_IMAGES = 4
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class InvalidImage(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedImage:
    sha256: str
    preview_sha256: str
    media_type: str
    width: int
    height: int
    preview: bytes


def validate_image(data: bytes, declared_type: str) -> ValidatedImage:
    if not data or len(data) > MAX_BYTES:
        raise InvalidImage("image_size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                media = FORMATS.get(image.format)
                if not media or declared_type not in (media, "application/octet-stream", ""):
                    raise InvalidImage("image_type")
                w, h = image.size
                if min(w, h) < 1 or max(w, h) > MAX_SIDE or w * h > MAX_PIXELS:
                    raise InvalidImage("image_dimensions")
                if getattr(image, "n_frames", 1) != 1:
                    raise InvalidImage("image_frames")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()  # Reject truncated images; never enable LOAD_TRUNCATED_IMAGES.
                oriented = ImageOps.exif_transpose(image)
                w, h = oriented.size
                # White backdrop for alpha, no EXIF/GPS/comments in model or UI preview.
                rgb = Image.new("RGB", oriented.size, "white")
                rgba = oriented.convert("RGBA")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                rgb.thumbnail((2048, 2048))
                out = io.BytesIO()
                rgb.save(out, format="JPEG", quality=90)
                preview = out.getvalue()
        return ValidatedImage(hashlib.sha256(data).hexdigest(), hashlib.sha256(preview).hexdigest(), media, w, h, preview)
    except InvalidImage:
        raise
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise InvalidImage("image_corrupt") from exc
