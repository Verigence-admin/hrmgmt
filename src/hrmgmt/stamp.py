"""Prints the evidence onto an attendance photo on the server: who, where, and when (the server's
IST time, never the phone's). The photo is decoded and re-encoded, so any hidden metadata is
dropped and only what the server draws is on the image."""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from hrmgmt.timeutil import to_ist

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_SIDE = 1280
_MAX_PIXELS = 40_000_000


class StampError(ValueError):
    """The upload is not an acceptable photo. The message is safe to show."""


def read_photo(data: bytes) -> Image.Image:
    if not data:
        raise StampError("Take a photo to continue.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise StampError("The photo is larger than 8 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise StampError("Use a JPEG, PNG or WebP photo.")
            if image.width * image.height > _MAX_PIXELS:
                raise StampError("The photo is too large in pixels.")
            image.load()
            return ImageOps.exif_transpose(image).convert("RGB")
    except StampError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise StampError("This file is not a readable photo.") from exc


def exif_capture_time(data: bytes) -> datetime | None:
    """When the camera says the picture was taken, if it says. Used only to flag, never to trust."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            raw = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def stamp_photo(
    photo: Image.Image,
    *,
    when: datetime,
    name: str,
    latitude: float,
    longitude: float,
    address: str | None,
    event: str,
) -> bytes:
    image = photo.copy()
    image.thumbnail((_MAX_SIDE, _MAX_SIDE))
    width, height = image.size
    size = max(14, width // 42)
    font = ImageFont.load_default(size=size)
    ist = to_ist(when)
    lines = [
        f"{event}  {ist:%d %b %Y  %H:%M:%S} IST",
        name,
        f"{latitude:.6f}, {longitude:.6f}",
    ]
    if address:
        lines.append(address)
    draw = ImageDraw.Draw(image)
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(_wrap(draw, line, font, width - 2 * size))
    line_height = int(size * 1.3)
    band = line_height * len(wrapped) + size
    overlay = Image.new("RGBA", (width, band), (0, 0, 0, 170))
    base = image.convert("RGBA")
    base.paste(overlay, (0, height - band), overlay)
    draw = ImageDraw.Draw(base)
    y = height - band + size // 2
    for line in wrapped:
        draw.text((size // 2, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height
    out = io.BytesIO()
    base.convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]
