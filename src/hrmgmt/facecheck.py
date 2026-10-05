"""Is there a person's face in the attendance photo? Only that: no one is identified or compared.
A photo of an object, a wall or a screen has no face, so it is flagged for HR to see. The check
runs here on the server with small bundled detectors: no outside service, no cost, no retry."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from PIL import Image, ImageOps

logger = structlog.get_logger(__name__)

_CHECK_SIDE = 640
# A face smaller than this share of the shorter side is too small to be the person taking the photo.
_MIN_FACE_SHARE = 0.10
# YuNet (OpenCV Zoo, MIT licence): finds tilted, looking-down, partly cut-off and backlit faces that the
# older detector below misses, and it does not take objects for faces. A face is accepted from this confidence.
_YUNET_MODEL = Path(__file__).parent / "face_models" / "face_detection_yunet_2023mar.onnx"
_YUNET_SCORE = 0.6


@lru_cache(maxsize=1)
def _yunet_ready() -> bool:
    """Whether the YuNet model file is there and loads. Checked once; if not, only the older detector runs."""
    try:
        import cv2

        cv2.FaceDetectorYN.create(str(_YUNET_MODEL), "", (320, 320), _YUNET_SCORE, 0.3, 10)
        return True
    except Exception:
        logger.warning("hr_face_check_unavailable", detector="yunet")
        return False


@lru_cache(maxsize=1)
def _detector() -> Any | None:
    try:
        import cv2

        # "alt2" is the more reliable of OpenCV's classic face detectors. It is used only if YuNet cannot load.
        found = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
        return None if found.empty() else found
    except Exception:
        logger.warning("hr_face_check_unavailable", detector="haar")
        return None


def _yunet_finds_face(photo: Image.Image) -> bool:
    import cv2
    import numpy as np

    small = ImageOps.exif_transpose(photo).convert("RGB")
    small.thumbnail((_CHECK_SIDE, _CHECK_SIDE))
    bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
    height, width = bgr.shape[:2]
    # A detector per photo: it holds the picture size, so one shared between simultaneous requests would race.
    detector = cv2.FaceDetectorYN.create(
        str(_YUNET_MODEL), "", (width, height), _YUNET_SCORE, 0.3, 10
    )
    _, faces = detector.detect(bgr)
    if faces is None:
        return False
    least = min(height, width) * _MIN_FACE_SHARE
    return any(min(float(face[2]), float(face[3])) >= least for face in faces)


def _haar_finds_face(photo: Image.Image, detector: Any) -> bool:
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


def face_present(photo: Image.Image) -> bool | None:
    """True or False, or None when the check could not run (then nothing is flagged)."""
    yunet = _yunet_ready()
    haar = _detector()
    if not yunet and haar is None:
        return None
    try:
        if yunet:
            return _yunet_finds_face(photo)
        return _haar_finds_face(photo, haar)
    except Exception:
        logger.warning("hr_face_check_failed")
        return None
