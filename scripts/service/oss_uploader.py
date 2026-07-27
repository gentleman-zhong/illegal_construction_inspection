"""OSS (S3-compatible) uploader for the algorithm service.

A thin wrapper over boto3 that hides endpoint / addressing-style / ACL
boilerplate. The uploader is built once at process start from a JSON
config file (default: ``oss_config.json`` next to this module) and
reused for every task.

Public surface:

* :class:`OssUploadError` — raised on any upload failure
* :func:`load_config` — read JSON config (env ``OSS_CONFIG`` overrides path)
* :class:`OssUploader` — ``upload_file`` / ``upload_directory``

Note on addressing style: 浪潮 (Inspur) OSS does not support
virtual-host style URLs (``<bucket>.<endpoint>/<key>``), so we force
``s3v4`` signing with ``addressing_style='path'`` (``<endpoint>/<bucket>/<key>``).
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

import boto3
from botocore.config import Config as BotoConfig


log = logging.getLogger("oss_uploader")


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "oss_config.json"

# Algorithm writes intermediate 3DTiles data into a `tmp/` subdir; the
# real tileset.json + .pnts live in 3DTiles/ directly. We always strip
# tmp/* from bulk uploads to avoid pushing stale / partial state.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = ("tmp/*",)


class OssUploadError(RuntimeError):
    """Raised on any upload failure. Callers should mark the task FAILED
    with the message so the backend can surface it to the user."""


def load_config(path: Optional[Path] = None) -> dict:
    """Load OSS config from JSON.

    Resolution order:

    1. ``OSS_CONFIG`` environment variable (absolute or relative path)
    2. ``path`` argument
    3. ``oss_config.json`` next to this module
    """
    env = os.getenv("OSS_CONFIG")
    if env:
        p = Path(env)
    elif path is not None:
        p = Path(path)
    else:
        p = DEFAULT_CONFIG_PATH
    if not p.is_file():
        raise FileNotFoundError(
            f"OSS config not found: {p}. "
            f"Copy oss_config.example.json next to api_server.py, or set "
            f"OSS_CONFIG=/path/to/oss_config.json."
        )
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Light validation so we fail fast at startup, not on first upload.
    for key in ("endpoint", "access_key", "secret_key", "bucket",
                "public_base", "key_prefix"):
        if key not in cfg:
            raise ValueError(f"OSS config missing required field: {key!r}")
    return cfg


class OssUploader:
    """Sync boto3 S3 client. Reusable across tasks.

    The client is thread-safe; the methods are synchronous and may be
    called from any thread (the algorithm service invokes them from
    the per-task reader thread, not the request thread)."""

    def __init__(self, config: dict, max_workers: int = 4):
        self.bucket        = config["bucket"]
        self.public_base   = config["public_base"].rstrip("/")
        self.key_prefix    = config["key_prefix"].strip("/")
        self.use_presigned = bool(config.get("use_presigned", False))
        self.max_workers   = max(1, int(config.get("max_workers", max_workers)))
        self.client = boto3.client(
            "s3",
            endpoint_url=config["endpoint"],
            aws_access_key_id=config["access_key"],
            aws_secret_access_key=config["secret_key"],
            region_name=config.get("region", "us-east-1"),
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )
        log.info("OssUploader init: bucket=%s endpoint=%s public_base=%s "
                 "key_prefix=%s presigned=%s",
                 self.bucket, config["endpoint"], self.public_base,
                 self.key_prefix, self.use_presigned)

    # ----- URL helpers -----

    def _object_url(self, remote_key: str) -> str:
        if self.use_presigned:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": remote_key},
                ExpiresIn=5 * 24 * 3600,  # 5 days
            )
        return f"{self.public_base}/{remote_key}"

    def make_key(self, *parts: str) -> str:
        """Join ``self.key_prefix`` with relative parts. Slashes are
        normalised, leading/trailing stripped. Returns a forward-slash
        object key, e.g. ``illegal-compare/TW-DEMO/3DTiles/tileset.json``."""
        all_parts = (self.key_prefix, *parts)
        joined = "/".join(p.strip("/") for p in all_parts)
        # Collapse accidental double-slashes from caller mistakes.
        while "//" in joined:
            joined = joined.replace("//", "/")
        return joined

    # ----- file operations -----

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        """Sync upload of a single file. Returns the public / presigned URL.

        Raises :class:`OssUploadError` on any failure. ACL is set to
        ``public-read`` so the URL is accessible without an ``?X-Amz-...``
        signature (only meaningful when ``use_presigned`` is False)."""
        try:
            self.client.upload_file(
                str(local_path), self.bucket, remote_key,
                ExtraArgs={"ACL": "public-read"},
            )
        except Exception as e:
            raise OssUploadError(
                f"upload failed: {local_path} -> "
                f"s3://{self.bucket}/{remote_key}: {e}"
            ) from e
        return self._object_url(remote_key)

    def upload_directory(self, local_dir: Path, remote_prefix: str,
                         exclude_globs: Iterable[str] = DEFAULT_EXCLUDE_GLOBS
                         ) -> list[str]:
        """Recursively upload every file under ``local_dir`` to
        ``<remote_prefix>/<relative_path>``.

        Files matching any pattern in ``exclude_globs`` (interpreted by
        :meth:`pathlib.Path.match`) are skipped. Returns the public URLs
        of every uploaded object (in completion order, not local order).

        Files are uploaded in parallel (default 4 workers). The function
        blocks until all uploads finish; on the first failure it
        re-raises :class:`OssUploadError`."""
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise OssUploadError(f"upload_directory: not a directory: {local_dir}")

        exclude = tuple(exclude_globs)
        files = [p for p in sorted(local_dir.rglob("*"))
                 if p.is_file()
                 and not any(p.match(g) for g in exclude)]

        if not files:
            log.warning("upload_directory: no files under %s (after excludes)",
                        local_dir)
            return []

        log.info("upload_directory: %d files from %s -> %s/",
                 len(files), local_dir, remote_prefix)

        urls: list[str] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            fut_to_path = {
                ex.submit(self.upload_file, p,
                          f"{remote_prefix.rstrip('/')}/"
                          f"{p.relative_to(local_dir).as_posix()}"): p
                for p in files
            }
            for fut in as_completed(fut_to_path):
                # If any one failed, the result() will re-raise and
                # propagate out of the with-block. Any other parallel
                # uploads in flight may finish / fail silently; that's
                # acceptable — we'll mark the task FAILED regardless.
                urls.append(fut.result())
        return urls


__all__ = ["OssUploadError", "load_config", "OssUploader", "DEFAULT_CONFIG_PATH"]
