"""Copies of backups on another drive or PC, optionally encrypted, and a restore test.

A copy is one file: `keirolake-<stamp>.zip`, or `keirolake-<stamp>.klb` when a
passphrase is set. The .klb format is the zip encrypted in 4 MB chunks with
AES-256-GCM; the key comes from the passphrase through scrypt. Every chunk is
authenticated, the chunk number is part of its nonce and the last chunk is
marked, so a truncated, reordered or altered file fails to open.

The restore test opens a copy (or a local backup) in a temporary folder without
touching live data: it checks the archive, the database's integrity and every
vector snapshot.
"""
from __future__ import annotations

import gzip
import json
import os
import secrets
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from pathlib import Path

from app.config import get_settings

MAGIC = b"KLB1"
CHUNK = 4 * 2**20
SCRYPT = {"n": 2**15, "r": 8, "p": 1}


class BackupCopyError(ValueError):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if len(passphrase) < 8:
        raise BackupCopyError("Use a passphrase of at least 8 characters")
    return Scrypt(salt=salt, length=32, **SCRYPT).derive(passphrase.encode())


def encrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(8)
    aead = AESGCM(_key(passphrase, salt))
    size = src.stat().st_size
    with src.open("rb") as fin, dest.open("wb") as fout:
        fout.write(MAGIC + salt + prefix)
        index, done = 0, 0
        while True:
            block = fin.read(CHUNK)
            done += len(block)
            last = done >= size
            sealed = aead.encrypt(prefix + struct.pack(">I", index), block, b"last" if last else b"more")
            fout.write(struct.pack(">I", len(sealed)) + sealed)
            index += 1
            if last:
                break


def decrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with src.open("rb") as fin, dest.open("wb") as fout:
        head = fin.read(28)
        if len(head) != 28 or head[:4] != MAGIC:
            raise BackupCopyError("This isn't a KeiroLake encrypted backup")
        aead = AESGCM(_key(passphrase, head[4:20]))
        prefix, index, finished = head[20:28], 0, False
        while True:
            raw_len = fin.read(4)
            if not raw_len:
                break
            if finished:
                raise BackupCopyError("The backup has extra data after its end")
            sealed = fin.read(struct.unpack(">I", raw_len)[0])
            nonce = prefix + struct.pack(">I", index)
            try:
                block = aead.decrypt(nonce, sealed, b"more")
            except InvalidTag:
                try:
                    block, finished = aead.decrypt(nonce, sealed, b"last"), True
                except InvalidTag:
                    raise BackupCopyError("Wrong passphrase, or the backup file is damaged") from None
            fout.write(block)
            index += 1
        if not finished:
            raise BackupCopyError("The backup file is cut short")


def _backup_folder(stamp: str) -> Path:
    from app.backup import backups_dir

    folder = backups_dir() / stamp
    if not (folder / "manifest.json").is_file() or "/" in stamp or "\\" in stamp or ".." in stamp:
        raise BackupCopyError("Backup not found")
    return folder


def copy_backup(stamp: str, target: str, passphrase: str = "", include_lake: bool = True) -> dict:
    """Write one backup (and optionally the lake files) as a single file into `target`."""
    folder = _backup_folder(stamp)
    dest_dir = Path(target).expanduser()
    if not dest_dir.is_dir():
        raise BackupCopyError(f"Folder not found: {dest_dir}. Plug in the drive or pick another folder.")
    lake = get_settings().lake_dir
    tmp_zip = dest_dir / f".keirolake-{stamp}.partial.zip"
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for f in sorted(folder.iterdir()):
                compress = zipfile.ZIP_STORED if f.suffix == ".gz" else zipfile.ZIP_DEFLATED
                zf.write(f, f"backup/{f.name}", compress_type=compress)
            if include_lake and lake.is_dir():
                for f in sorted(lake.rglob("*")):
                    if f.is_file():
                        zf.write(f, f"lake/{f.relative_to(lake).as_posix()}")
        if passphrase:
            final = dest_dir / f"keirolake-{stamp}.klb"
            encrypt_file(tmp_zip, final, passphrase)
            tmp_zip.unlink()
        else:
            final = dest_dir / f"keirolake-{stamp}.zip"
            os.replace(tmp_zip, final)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise
    return {"path": str(final), "bytes": final.stat().st_size, "encrypted": bool(passphrase), "includes_lake": include_lake}


def verify(*, stamp: str | None = None, path: str | None = None, passphrase: str = "") -> dict:
    """Open a backup in a temporary folder and check it can be restored. Live data is not touched."""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    with tempfile.TemporaryDirectory(prefix="keirolake-verify-") as tmp:
        work = Path(tmp)
        if stamp:
            root = _backup_folder(stamp)
            check("Backup folder", True, root.name)
        else:
            src = Path(path or "").expanduser()
            if not src.is_file():
                raise BackupCopyError(f"File not found: {src}")
            archive = src
            if src.suffix == ".klb":
                if not passphrase:
                    raise BackupCopyError("This backup is encrypted: enter its passphrase")
                archive = work / "backup.zip"
                decrypt_file(src, archive, passphrase)
                check("Decrypted with the passphrase", True)
            try:
                with zipfile.ZipFile(archive) as zf:
                    bad = zf.testzip()
                    check("Archive is intact", bad is None, f"damaged member: {bad}" if bad else f"{len(zf.namelist())} files")
                    zf.extractall(work / "x")
            except zipfile.BadZipFile as exc:
                raise BackupCopyError(f"Not a readable backup: {exc}") from exc
            root = work / "x" / "backup"
            lake_files = list((work / "x" / "lake").rglob("*")) if (work / "x" / "lake").is_dir() else []
            check("Lake files", True, f"{sum(1 for f in lake_files if f.is_file())} files" if lake_files else "not included")

        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
        check("Manifest", manifest is not None, f"{len(manifest['knowledge_bases'])} knowledge bases" if manifest else "missing")

        db = root / "keirolake.db"
        if db.is_file():
            copy = work / "check.db"
            shutil.copyfile(db, copy)
            conn = sqlite3.connect(copy)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] if tables else 0
                check("Database integrity", result == "ok", f"{tables} tables, {docs} documents" if result == "ok" else result)
            except sqlite3.Error as exc:
                check("Database integrity", False, str(exc))
            finally:
                conn.close()
        else:
            check("Database integrity", False, "keirolake.db missing")

        for snap in sorted(root.glob("*.snapshot.gz")):
            try:
                with gzip.open(snap, "rb") as fh:
                    while fh.read(CHUNK):
                        pass
                check(f"Vectors: {snap.name.removesuffix('.snapshot.gz')}", True, f"{snap.stat().st_size // 1024} KB")
            except (OSError, EOFError) as exc:
                check(f"Vectors: {snap.name}", False, str(exc))
        for points in sorted(root.glob("*.points.jsonl")):
            check(f"Vectors: {points.name.removesuffix('.points.jsonl')}", points.stat().st_size > 0)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
