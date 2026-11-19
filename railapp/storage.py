"""Content-addressed, immutable object store plus startup reconciliation.

Every published file (the original upload and both downstream CSVs) is stored
as a blob named by its SHA-256.  Blobs are written once and never modified or
deleted while the process runs, so an already-started download always reads a
complete, immutable file even if a newer revision is published meanwhile.

The SQLite publication row is the single source of truth for which three
blobs form a revision.  Downloads resolve the row first and then open blobs;
they never discover files by scanning directories, so a crash halfway through
a publish can never make a reader mix revisions.
"""

import hashlib
import os
import shutil
import tempfile

from .errors import Unavailable

_READ_CHUNK = 1024 * 1024


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class BlobStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.objects = os.path.join(self.root, "objects")
        self.tmp = os.path.join(self.root, "tmp")
        os.makedirs(self.objects, exist_ok=True)
        os.makedirs(self.tmp, exist_ok=True)

    # -- paths -----------------------------------------------------------
    def path_for(self, digest):
        if not isinstance(digest, str) or len(digest) != 64:
            raise Unavailable("对象标识无效")
        return os.path.join(self.objects, digest[:2], digest[2:])

    def exists(self, digest):
        try:
            return os.path.isfile(self.path_for(digest))
        except Unavailable:
            return False

    # -- writes ----------------------------------------------------------
    def put_bytes(self, data):
        digest = hashlib.sha256(data).hexdigest()
        final = self.path_for(digest)
        if os.path.exists(final):
            return digest
        directory = os.path.dirname(final)
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="blob-", dir=self.tmp)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final)
            _fsync_dir(directory)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return digest

    def put_file(self, source_path):
        digest = hashlib.sha256()
        fd, tmp_path = tempfile.mkstemp(prefix="blob-", dir=self.tmp)
        try:
            with open(source_path, "rb") as src, os.fdopen(fd, "wb") as dst:
                while True:
                    chunk = src.read(_READ_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            hexdigest = digest.hexdigest()
            final = self.path_for(hexdigest)
            if os.path.exists(final):
                os.unlink(tmp_path)
                return hexdigest
            directory = os.path.dirname(final)
            os.makedirs(directory, exist_ok=True)
            os.replace(tmp_path, final)
            _fsync_dir(directory)
            return hexdigest
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- reads -----------------------------------------------------------
    def open_read(self, digest):
        """Open a blob for streaming.  Raises Unavailable if it is missing."""
        path = self.path_for(digest)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise Unavailable(
                "该版本的已发布文件缺失，无法提供完整一致的下载；请联系管理员"
            ) from None

    def read_bytes(self, digest):
        with self.open_read(digest) as handle:
            return handle.read()

    def verify(self, digest):
        path = self.path_for(digest)
        try:
            with open(path, "rb") as handle:
                hasher = hashlib.sha256()
                while True:
                    chunk = handle.read(_READ_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest() == digest
        except FileNotFoundError:
            return False

    # -- housekeeping ----------------------------------------------------
    def clean_tmp(self):
        removed = 0
        for entry in os.scandir(self.tmp):
            try:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)
                removed += 1
            except OSError:
                pass
        return removed


def verify_publications(conn, store):
    """Return a list of publication records with missing/corrupt blobs.

    Called on startup; a damaged revision is never replaced by another one,
    it is simply reported and its downloads fail closed with 503.
    """
    problems = []
    rows = conn.execute(
        """SELECT day, rev, original_sha256, settlement_sha256, ridership_sha256
           FROM publications ORDER BY day, rev"""
    ).fetchall()
    for row in rows:
        missing = [
            digest
            for digest in (
                row["original_sha256"],
                row["settlement_sha256"],
                row["ridership_sha256"],
            )
            if not store.exists(digest)
        ]
        if missing:
            problems.append({"day": row["day"], "rev": row["rev"], "missing": missing})
    return problems
