"""Is there a person's face in the attendance photo? Only that: no one is identified or compared.
A photo of an object, a wall or a screen has no face, so it is flagged for HR to see. The check
runs here on the server with a small bundled detector: no outside service, no cost, no retry."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog
from PIL import Image, ImageOps

logger = structlog.get_logger(__name__)

_CHECK_SIDE = 640
# A face smaller than this share of the shorter side is too small to be the person taking the photo.
_MIN_FACE_SHARE = 0.10


@lru_cache(maxsize=1)
def _detector() -> Any | None:
    try:
        import cv2

        # "alt2" finds real selfies (tilted, dim, backlit) far more reliably than the default one, and it
        # does not mistake ceilings or round objects for faces.
        found = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
        return None if found.empty() else found
    except Exception:
        logger.warning("hr_face_check_unavailable")
        return None


def face_present(photo: Image.Image) -> bool | None:
    """True or False, or None when the check could not run (then nothing is flagged)."""
    detector = _detector()
    if detector is None:
        return None
    try:
        import cv2
        import numpy as np

        small = ImageOps.grayscale(photo)
        small.thumbnail((_CHECK_SIDE, _CHECK_SIDE))
        grey = np.asarray(small)
        least = max(24, int(min(grey.shape[:2]) * _MIN_FACE_SHARE))
        # Local contrast first (handles a face in shadow against a bright window), then the plain
        # picture. The first pass that finds a face is enough, so a normal photo costs one pass.
        for prepared in (cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(grey), grey):
            faces = detector.detectMultiScale(
                prepared, scaleFactor=1.1, minNeighbors=4, minSize=(least, least)
            )
            if len(faces) > 0:
                return True
        return False
    except Exception:
        logger.warning("hr_face_check_failed")
        return None
