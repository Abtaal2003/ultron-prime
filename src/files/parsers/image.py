"""Images: validation-time decoding, metadata stripping, and model-bound downscaling."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from src.files.parsers import ParsedDoc

MAX_MODEL_EDGE = 1568
Image.MAX_IMAGE_PIXELS = 50_000_000


def parse_image(data: bytes) -> ParsedDoc:
    """Describe an image. Pixel content is only read by a vision model, never OCR'd here."""
    with Image.open(io.BytesIO(data)) as img:
        doc = ParsedDoc(family="image", pages=1)
        doc.meta = {"width": img.width, "height": img.height, "format": img.format or ""}
        frames = getattr(img, "n_frames", 1)
        if frames > 1:
            doc.warnings.append("animated image: only the first frame is used")
    return doc


def normalise_image(data: bytes) -> tuple[bytes, str]:
    """Re-encode as PNG (JPEG stays JPEG) without EXIF/ICC/text metadata; first frame only."""
    with Image.open(io.BytesIO(data)) as img:
        fmt = (img.format or "PNG").upper()
        img.seek(0)
        fixed: Image.Image = ImageOps.exif_transpose(img)
        out = io.BytesIO()
        if fmt == "JPEG":
            fixed.convert("RGB").save(out, format="JPEG", quality=92)
            return out.getvalue(), "image/jpeg"
        if fixed.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            fixed = fixed.convert("RGBA")
        fixed.save(out, format="PNG")
        return out.getvalue(), "image/png"


def downscale_for_model(data: bytes, max_edge: int = MAX_MODEL_EDGE) -> tuple[bytes, str]:
    """Return a copy no larger than ``max_edge`` on its long side (the original is untouched)."""
    with Image.open(io.BytesIO(data)) as img:
        fmt = (img.format or "PNG").upper()
        img.thumbnail((max_edge, max_edge))
        out = io.BytesIO()
        if fmt == "JPEG":
            img.convert("RGB").save(out, format="JPEG", quality=88)
            return out.getvalue(), "image/jpeg"
        img.save(out, format="PNG")
        return out.getvalue(), "image/png"
