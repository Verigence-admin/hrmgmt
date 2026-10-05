"""The face numbers HR keeps: for the profile photo, worked out when the photo is set (or the first
time it is needed, for photos set before this existed), and for each day's check-in photo."""

from __future__ import annotations

import io

import structlog
from PIL import Image
from sqlalchemy import Connection, text

from hrmgmt.facematch import MODEL_ID, embed
from hrmgmt.storage import ObjectStorage, StorageError

logger = structlog.get_logger(__name__)


def profile_photo_key(employee_id: str) -> str:
    return f"employee/{employee_id}/photo.jpg"


def save_profile_face(conn: Connection, employee_id: str, numbers: bytes | None) -> None:
    """Keeps the face numbers of the employee's CURRENT profile photo (None: no clear face in it)."""
    conn.execute(
        text(
            "INSERT INTO hr.employee_face (employee_id, model, embedding, photo_updated_at)"
            " SELECT employee_id, :m, :n, photo_updated_at FROM hr.employee"
            " WHERE employee_id = CAST(:e AS uuid) AND photo_updated_at IS NOT NULL"
            " ON CONFLICT (employee_id) DO UPDATE SET model = EXCLUDED.model,"
            " embedding = EXCLUDED.embedding, photo_updated_at = EXCLUDED.photo_updated_at,"
            " computed_at = now()"
        ),
        {"e": employee_id, "m": MODEL_ID, "n": numbers},
    )


def profile_face(conn: Connection, storage: ObjectStorage | None, employee_id: str) -> bytes | None:
    """The face numbers of the employee's profile photo, or None when there is no photo or no clear
    face in it. Photos set before face matching existed are worked out here, once."""
    row = (
        conn.execute(
            text(
                "SELECT e.photo_updated_at AS photo_at, f.embedding, f.model,"
                " f.photo_updated_at AS face_at, (f.employee_id IS NOT NULL) AS has_row"
                " FROM hr.employee e LEFT JOIN hr.employee_face f ON f.employee_id = e.employee_id"
                " WHERE e.employee_id = CAST(:e AS uuid)"
            ),
            {"e": employee_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["photo_at"] is None:
        return None
    if row["has_row"] and row["model"] == MODEL_ID and row["face_at"] == row["photo_at"]:
        return bytes(row["embedding"]) if row["embedding"] is not None else None
    if storage is None:
        return None
    try:
        data = storage.get(profile_photo_key(employee_id))
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            numbers = embed(image)
    except (StorageError, OSError, ValueError):
        logger.warning("hr_face_profile_unreadable")
        return None
    save_profile_face(conn, employee_id, numbers)
    return numbers
