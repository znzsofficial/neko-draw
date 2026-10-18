"""Small signed Life admission boundary; durable consume-before-execute ledger."""

import hashlib
from contextlib import closing
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def authenticate(packet, directory):
    try:
        path = Path(directory) / "life-keys.json"
        if (
            path.is_symlink()
            or path.stat().st_size > 1024
            or (os.name == "posix" and path.stat().st_mode & 0o077)
        ):
            raise ValueError
        key = json.loads(path.read_text(encoding="utf-8"))["neko.life"]
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key):
            raise ValueError
        if not isinstance(packet, dict) or set(packet) != {
            "v",
            "client",
            "audience",
            "issued",
            "nonce",
            "operation",
            "body",
            "signature",
        }:
            raise ValueError
        if (
            type(packet["v"]) is not int
            or packet["v"] != 1
            or packet["client"] != "neko.life"
            or packet["audience"] != "neko.draw"
            or type(packet["issued"]) is not int
            or abs(time.time() - packet["issued"]) > 120
            or packet["operation"] != "submit"
            or not isinstance(packet["nonce"], str)
            or not re.fullmatch("[a-f0-9]{32}", packet["nonce"])
            or not isinstance(packet["signature"], str)
            or not re.fullmatch("[a-f0-9]{64}", packet["signature"])
        ):
            raise ValueError
        raw = json.dumps(
            {k: v for k, v in packet.items() if k != "signature"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(raw) > 6000 or not hmac.compare_digest(
            packet["signature"],
            hmac.new(bytes.fromhex(key), raw, hashlib.sha256).hexdigest(),
        ):
            raise ValueError
        body = packet["body"]
        if not isinstance(body, dict) or set(body) != {
            "session_id",
            "request_id",
            "prompt",
        }:
            raise ValueError
        if (
            not isinstance(body["session_id"], str)
            or not 1 <= len(body["session_id"]) <= 128
            or not isinstance(body["request_id"], str)
            or not re.fullmatch("[a-f0-9]{32}", body["request_id"])
            or not isinstance(body["prompt"], str)
            or not 1 <= len(body["prompt"]) <= 1000
        ):
            raise ValueError
        return body, packet["nonce"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError("unauthorized") from None


def consume(directory, body, nonce, clock=time.time):
    """Original packet replay rejects; re-signed request returns its old task ID."""
    path = Path(directory) / "life-admission.db"
    if path.is_symlink():
        raise ValueError("unsafe_database")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("corrupt_database_preserved")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("unknown_schema_preserved")
        db.executescript("""CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,session TEXT,digest TEXT,created REAL);
                           CREATE TABLE IF NOT EXISTS nonces(id TEXT PRIMARY KEY,created REAL); PRAGMA user_version=1;""")
        path.chmod(0o600)
        db.execute("BEGIN IMMEDIATE")
        now = clock()
        db.execute("DELETE FROM nonces WHERE created<?", (now - 600,))
        # Never prune request IDs: a previously accepted request cannot be
        # replayed after archival. Fail closed on capacity, not silent eviction.
        if db.execute("SELECT count(*) FROM nonces").fetchone()[0] >= 8192:
            raise ValueError("admission_busy")
        try:
            db.execute("INSERT INTO nonces VALUES (?,?)", (nonce, now))
        except sqlite3.IntegrityError:
            raise ValueError("replay_rejected") from None
        digest = hashlib.sha256(body["prompt"].encode()).hexdigest()
        old = db.execute(
            "SELECT session,digest FROM requests WHERE id=?", (body["request_id"],)
        ).fetchone()
        if old:
            if old != (body["session_id"], digest):
                raise ValueError("idempotency_conflict")
            return False
        if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= 1000000:
            raise ValueError("admission_capacity")
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            (body["request_id"], body["session_id"], digest, now),
        )
        return True
