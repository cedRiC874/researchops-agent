from __future__ import annotations

import hashlib

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..domain import ObjectOutcomeUnknown, StoredObject, TransientDependencyError


class S3ObjectStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        region_name: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        server_side_encryption: bool,
    ) -> None:
        self._bucket = bucket
        self._server_side_encryption = server_side_encryption
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                retries={"max_attempts": 1, "mode": "standard"},
                signature_version="s3v4",
            ),
        )

    def initialize_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            status_code = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if status_code not in {404, 400}:
                raise TransientDependencyError("无法检查 object bucket。") from exc
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except ClientError as create_exc:
                create_status = int(
                    create_exc.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode", 0
                    )
                )
                if create_status != 409:
                    raise TransientDependencyError("无法创建 object bucket。") from create_exc
                self._client.head_bucket(Bucket=self._bucket)

    def healthcheck(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except (BotoCoreError, ClientError):
            return False

    def put_json(
        self, *, object_key: str, payload: bytes, sha256: str
    ) -> StoredObject:
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise ObjectOutcomeUnknown("写入前 payload hash 不匹配。")
        try:
            arguments = {
                "Bucket": self._bucket,
                "Key": object_key,
                "Body": payload,
                "ContentType": "application/json",
                "Metadata": {"sha256": sha256, "byte-size": str(len(payload))},
            }
            if self._server_side_encryption:
                arguments["ServerSideEncryption"] = "AES256"
            self._client.put_object(
                **arguments,
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectOutcomeUnknown("对象写入结果无法确认。") from exc
        return StoredObject(object_key, sha256, len(payload))

    def get_json(self, *, object_key: str, expected_sha256: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            payload = response["Body"].read()
        except (BotoCoreError, ClientError, OSError) as exc:
            raise TransientDependencyError("无法读取聚合结果对象。") from exc
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ObjectOutcomeUnknown("对象读取 hash 不匹配。")
        return payload

    def head_json(self, *, object_key: str) -> StoredObject | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            status_code = int(
                exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            )
            if status_code == 404:
                return None
            raise TransientDependencyError("无法核对对象状态。") from exc
        except BotoCoreError as exc:
            raise TransientDependencyError("无法核对对象状态。") from exc
        metadata = response.get("Metadata", {})
        sha256 = metadata.get("sha256")
        byte_text = metadata.get("byte-size")
        try:
            byte_size = int(byte_text)
        except (TypeError, ValueError) as exc:
            raise ObjectOutcomeUnknown("对象 metadata 无效。") from exc
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or int(response.get("ContentLength", -1)) != byte_size
        ):
            raise ObjectOutcomeUnknown("对象 metadata 与长度不一致。")
        return StoredObject(object_key, sha256, byte_size)
