"""
Server-side, in-memory store mapping an opaque session token to the
unwrapped tenant DEK for that session (see schema.sql MODULE T and
security/crypto.py — the DEK belongs to the logged-in user's TENANT, shared
by every user of that tenant, not derived from that user's password).

This is the right amount of machinery for a single-process, locally-run
Phase 1 deployment. It intentionally does NOT put the DEK in the Flask
session cookie: the cookie is signed (tamper-evident) but not encrypted, so
its contents are readable by anyone with the cookie — not where a field
encryption key should live.

If this app is ever run multi-process (e.g. behind gunicorn with >1 worker)
or across restarts-without-relogin, this in-memory dict stops being enough —
swap it for a proper server-side session store (e.g. a short-lived table
keyed by a hashed token, or an OS keyring) at that point. Flagged here
rather than silently outgrown.
"""
import threading
import time

from config import Config

_lock = threading.Lock()
_store = {}  # token -> {"dek": bytes, "expires_at": float}


def put(token: str, dek: bytes) -> None:
    with _lock:
        _store[token] = {
            "dek": dek,
            "expires_at": time.time() + Config.SESSION_TIMEOUT_MINUTES * 60,
        }


def get(token: str):
    """Returns the DEK bytes if the token is present and unexpired, else None.
    A successful read refreshes the expiry (sliding session timeout)."""
    with _lock:
        entry = _store.get(token)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            del _store[token]
            return None
        entry["expires_at"] = time.time() + Config.SESSION_TIMEOUT_MINUTES * 60
        return entry["dek"]


def drop(token: str) -> None:
    with _lock:
        _store.pop(token, None)
