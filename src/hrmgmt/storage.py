from __future__ import annotations

from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)


class StorageError(RuntimeError):
    """The object store did not complete the request."""


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...


class S3Storage:
    """The existing S3-compatible bucket, used only under the `hr/` prefix. Nothing here is
    public; files leave only through HRMgmt after its permission checks."""

    PREFIX = "hr/"

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        if client is not None:
            self._client = client
            return
        import boto3
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                connect_timeout=5, read_timeout=15, retries={"max_attempts": 1, "mode": "standard"}
            ),
        )

    def _full_key(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise StorageError("invalid object key")
        return self.PREFIX + key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._full_key(key), Body=data, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("hr_storage_put_failed", error_type=type(exc).__name__)
            raise StorageError("could not store the file") from exc

    def get(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
            body: bytes = response["Body"].read()
            return body
        except (BotoCoreError, ClientError) as exc:
            logger.warning("hr_storage_get_failed", error_type=type(exc).__name__)
            raise StorageError("could not read the file") from exc
