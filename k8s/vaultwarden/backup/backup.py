"""Encrypted, consistent backup of Vaultwarden's data directory.

SQLite is copied with the online backup API (safe while Vaultwarden runs),
checked with integrity_check, archived, encrypted to an age recipient, and
uploaded to S3-compatible storage. Old objects beyond the retention window
are pruned. Exits non-zero on any failure so the CronJob shows as failed.
"""
import datetime
import os
import pathlib
import sqlite3
import tarfile
import tempfile
import time

import boto3
import pyrage
from pyrage import x25519

DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
ENDPOINT = os.environ.get("S3_ENDPOINT", "")
BUCKET = os.environ.get("S3_BUCKET", "backups")
PREFIX = os.environ.get("S3_PREFIX", "vaultwarden/")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "14"))
RECIPIENT = x25519.Recipient.from_str(os.environ["AGE_RECIPIENT"])
BACKUP_DIR = os.environ.get("BACKUP_DIR", "")


def snapshot_db(workdir: pathlib.Path) -> pathlib.Path:
    dst_path = workdir / "db.sqlite3"
    src = sqlite3.connect(DATA / "db.sqlite3")
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(dst_path)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise SystemExit(f"integrity_check failed: {result}")
    return dst_path


def build_archive(workdir: pathlib.Path, stamp: str) -> pathlib.Path:
    archive = workdir / f"vaultwarden-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(snapshot_db(workdir), arcname="db.sqlite3")
        for name in ("rsa_key.pem", "config.json"):
            path = DATA / name
            if path.exists():
                tar.add(path, arcname=name)
        attachments = DATA / "attachments"
        if attachments.exists():
            tar.add(attachments, arcname="attachments")
    return archive


def encrypt(path: pathlib.Path) -> bytes:
    return pyrage.encrypt(path.read_bytes(), [RECIPIENT])


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["ACCESS_KEY"],
        aws_secret_access_key=os.environ["SECRET_KEY"],
        region_name="us-east-1",
    )


def prune(s3) -> int:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RETENTION_DAYS)
    removed = 0
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX).get("Contents", []):
        if obj["LastModified"] < cutoff:
            s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
            removed += 1
    return removed


def write_local(blob: bytes, name: str) -> None:
    if not BACKUP_DIR:
        return
    target = pathlib.Path(BACKUP_DIR) / name
    target.write_bytes(blob)
    if target.stat().st_size != len(blob):
        raise SystemExit("size mismatch on local copy")


def prune_local() -> int:
    if not BACKUP_DIR:
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for path in pathlib.Path(BACKUP_DIR).glob("vaultwarden-*.tar.gz.age"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def main() -> None:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        blob = encrypt(build_archive(workdir, stamp))
    key = f"{PREFIX}vaultwarden-{stamp}.tar.gz.age"
    s3 = s3_client()
    s3.put_object(Bucket=BUCKET, Key=key, Body=blob)
    if s3.head_object(Bucket=BUCKET, Key=key)["ContentLength"] != len(blob):
        raise SystemExit("size mismatch after upload")
    removed = prune(s3)
    write_local(blob, key.rsplit("/", 1)[-1])
    local_removed = prune_local()
    print(f"ok key={key} bytes={len(blob)} pruned={removed} local_pruned={local_removed}")


if __name__ == "__main__":
    main()
