"""商品图片的受限本地存储；生产环境应挂载持久卷或替换为对象存储适配器。"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"RIFF", "image/webp", ".webp"),
)


def upload_root() -> Path:
    configured = os.getenv("PRODUCT_UPLOAD_DIR", "data/runtime/uploads/products")
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def image_type(prefix: bytes) -> tuple[str, str] | None:
    for signature, content_type, extension in _IMAGE_SIGNATURES:
        if prefix.startswith(signature) and (
            content_type != "image/webp" or len(prefix) >= 12 and prefix[8:12] == b"WEBP"
        ):
            return content_type, extension
    return None


async def save_image(upload: UploadFile) -> tuple[str, str, int]:
    """流式保存图片，并以内容头而非客户端扩展名决定类型。"""
    declared_type = (upload.content_type or "").lower()
    if declared_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="unsupported image type")
    root = upload_root()
    temporary = root / f".{uuid4().hex}.uploading"
    size_bytes = 0
    prefix = b""
    try:
        with temporary.open("xb") as destination:
            while chunk := await upload.read(64 * 1024):
                size_bytes += len(chunk)
                if size_bytes > MAX_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="image exceeds 5 MiB limit")
                if len(prefix) < 16:
                    prefix += chunk[: 16 - len(prefix)]
                destination.write(chunk)
        detected = image_type(prefix)
        if detected is None or detected[0] != declared_type:
            raise HTTPException(status_code=422, detail="invalid image content")
        storage_key = f"{uuid4().hex}{detected[1]}"
        temporary.replace(root / storage_key)
        return storage_key, detected[0], size_bytes
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def delete_image(storage_key: str) -> None:
    (upload_root() / storage_key).unlink(missing_ok=True)


def image_path(storage_key: str) -> Path | None:
    root = upload_root()
    candidate = (root / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
