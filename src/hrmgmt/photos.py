from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_MAX_SIDE_PIXELS = 12_000
_OUTPUT_SIDE = 512
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class PhotoError(ValueError):
    """The uploaded file is not an acceptable profile photo (message is safe to show)."""


def normalise_profile_photo(data: bytes) -> bytes:
    """Validate by actually decoding the image, then re-encode as a 512 px JPEG. Re-encoding drops
    all embedded metadata (including GPS position), so only the picture is kept."""
    if not data:
        raise PhotoError("Choose a photo to upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("The photo is larger than 5 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _ALLOWED_FORMATS:
                raise PhotoError("Use a JPEG, PNG or WebP photo.")
            if max(image.size) > _MAX_SIDE_PIXELS:
                raise PhotoError("The photo is too large in pixels.")
            image.load()
            photo = ImageOps.exif_transpose(image).convert("RGB")
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PhotoError("This file is not a readable photo.") from exc
    photo.thumbnail((_OUTPUT_SIDE, _OUTPUT_SIDE))
    out = io.BytesIO()
    photo.save(out, format="JPEG", quality=85)
    return out.getvalue()
