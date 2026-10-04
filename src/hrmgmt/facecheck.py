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
_MIN_FACE_SHARE = 0.12


@lru_cache(maxsize=1)
def _detector() -> Any | None:
    try:
        import cv2

        found = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
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
        import numpy as np

        small = ImageOps.grayscale(photo)
        small.thumbnail((_CHECK_SIDE, _CHECK_SIDE))
        grey = np.asarray(small)
        import cv2

        grey = cv2.equalizeHist(grey)
        least = max(24, int(min(grey.shape[:2]) * _MIN_FACE_SHARE))
        faces = detector.detectMultiScale(
            grey, scaleFactor=1.1, minNeighbors=4, minSize=(least, least)
        )
        return len(faces) > 0
    except Exception:
        logger.warning("hr_face_check_failed")
        return None
