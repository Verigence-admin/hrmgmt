"""Does the face in a photo look like the face in another photo? Runs here on the server with two small
bundled models (a detector and SFace, a face-matching model): no outside service, no cost, no retry.

A face becomes 128 numbers; two photos of the same person give numbers that point the same way. The
score is their cosine similarity: 1 is identical, near 0 is a different person. This only ever flags
for HR to look; nobody is refused because of it."""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from PIL import Image, ImageOps

logger = structlog.get_logger(__name__)

# What the stored numbers were made with. Numbers from another model are never compared with these.
MODEL_ID = "sface-2021dec-int8"
# The model makers' own cut-off for "same person" (cosine). HR can change it in Settings.
DEFAULT_THRESHOLD = 0.363

_MODELS = Path(__file__).parent / "face_models"
_DETECTOR = _MODELS / "face_detection_yunet_2023mar.onnx"
_RECOGNIZER = _MODELS / "face_recognition_sface_2021dec_int8.onnx"
_SIDE = 960
# The person taking the photo fills a good part of it; a tiny face in the background is not them.
_MIN_FACE_SHARE = 0.10
_DETECT_SCORE = 0.6
_local = threading.local()


@lru_cache(maxsize=1)
def _ready() -> bool:
    """Whether both model files are there and load. Checked once; if not, nothing is ever compared."""
    try:
        import cv2

        cv2.FaceDetectorYN.create(str(_DETECTOR), "", (320, 320), _DETECT_SCORE, 0.3, 10)
        cv2.FaceRecognizerSF.create(str(_RECOGNIZER), "")
        return True
    except Exception:
        logger.warning("hr_face_match_unavailable")
        return False


def _recognizer() -> Any:
    # One per thread: requests run in parallel threads and a model object is not shared between them.
    found = getattr(_local, "recognizer", None)
    if found is None:
        import cv2

        found = cv2.FaceRecognizerSF.create(str(_RECOGNIZER), "")
        _local.recognizer = found
    return found


def embed(photo: Image.Image) -> bytes | None:
    """The face numbers of the main face in the photo, or None when there is no clear face or the
    check could not run. The main face is the biggest one."""
    if not _ready():
        return None
    try:
        import cv2
        import numpy as np

        rgb = ImageOps.exif_transpose(photo).convert("RGB")
        rgb.thumbnail((_SIDE, _SIDE))
        bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
        height, width = bgr.shape[:2]
        # A detector per photo: it holds the picture size, so one shared between requests would race.
        detector = cv2.FaceDetectorYN.create(
            str(_DETECTOR), "", (width, height), _DETECT_SCORE, 0.3, 10
        )
        _, faces = detector.detect(bgr)
        if faces is None:
            return None
        least = min(height, width) * _MIN_FACE_SHARE
        big = [f for f in faces if min(float(f[2]), float(f[3])) >= least]
        if not big:
            return None
        main = max(big, key=lambda f: float(f[2]) * float(f[3]))
        recognizer = _recognizer()
        numbers = recognizer.feature(recognizer.alignCrop(bgr, main))
        return np.asarray(numbers, dtype=np.float32).reshape(-1).tobytes()
    except Exception:
        logger.warning("hr_face_match_failed")
        return None


def similarity(first: bytes, second: bytes) -> float | None:
    """Cosine similarity of two sets of face numbers; None if either is not a valid set."""
    import numpy as np

    a = np.frombuffer(first, dtype=np.float32)
    b = np.frombuffer(second, dtype=np.float32)
    if a.size == 0 or a.size != b.size:
        return None
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0:
        return None
    return round(float(np.dot(a, b)) / norm, 3)
