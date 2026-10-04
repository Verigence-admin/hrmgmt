"""Receipt files for claims. Images are decoded and re-encoded (which drops hidden metadata such
as GPS), PDFs are accepted as they are after a signature check. Nothing else is accepted."""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
MAX_RECEIPTS_PER_CLAIM = 5
_MAX_SIDE = 1600
_MAX_PIXELS = 40_000_000


class ReceiptError(ValueError):
    """The file is not an acceptable receipt. The message is safe to show."""


def normalise_receipt(data: bytes) -> tuple[bytes, str, str]:
    """Returns (bytes to store, content type, file extension)."""
    if not data:
        raise ReceiptError("Choose a receipt file.")
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptError("A receipt is larger than 5 MB.")
    if data[:5] == b"%PDF-":
        return data, "application/pdf", "pdf"
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ReceiptError("Receipts must be a photo (JPEG, PNG, WebP) or a PDF.")
            if image.width * image.height > _MAX_PIXELS:
                raise ReceiptError("The photo is too large in pixels.")
            image.load()
            photo = ImageOps.exif_transpose(image).convert("RGB")
    except ReceiptError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ReceiptError("This file is not a readable photo or PDF.") from exc
    photo.thumbnail((_MAX_SIDE, _MAX_SIDE))
    out = io.BytesIO()
    photo.save(out, format="JPEG", quality=85)
    return out.getvalue(), "image/jpeg", "jpg"
