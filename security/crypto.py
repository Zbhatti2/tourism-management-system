"""
Field-level encryption for sensitive columns.

Split into two independent concerns for multi-tenant, multi-user Phase 1.1
(see schema.sql MODULE T for the full rationale):

  * Per-user LOGIN password checking is handled by security/passwords.py —
    a standard salted hash, nothing to do with encryption keys.
  * Per-TENANT field encryption is handled here. Each tenant gets its own
    random Data Encryption Key (DEK), generated once when the tenant is
    created (see new_tenant_dek() below) and wrapped/unwrapped for that
    tenant's sensitive columns as the schema grows. As of Phase 1.1's
    Points of Interest pivot there are no encrypted columns left in the
    schema (the "Platforms & Subscriptions" and "Accounts" modules that
    used to hold them — cloud_service_subscriptions.password,
    personal_accounts.account_number/cvv/pin/password,
    desktop_software_licenses.serial_number — were removed), but the
    encrypt_value()/decrypt_value() utilities below stay in place for
    whatever sensitive column comes next.

    The DEK is never stored in the clear — it is wrapped (encrypted) once,
    under the system-level tenant master key (Config.get_tenant_master_key(),
    persisted to instance/tenant_master.key). That wrap/unwrap is a server
    operation: it happens once a user's password has already been verified
    (security/passwords.py), not derived from the password itself. This is
    what lets several different users of one tenant — and a Tenant Admin
    resetting a teammate's password — all read the same encrypted data
    without any re-encryption.

    The unwrapped DEK lives only in server-side, in-memory session storage
    (security/session_keys.py) for the life of the session — never in the
    database, a config file, or the browser (the session cookie only
    carries an opaque session id).
"""
import base64

from cryptography.fernet import Fernet, InvalidToken

from config import Config


def new_tenant_dek() -> bytes:
    """A fresh random Fernet key — the actual field-encryption key for one tenant."""
    return Fernet.generate_key()


def wrap_tenant_dek(dek: bytes) -> bytes:
    """Encrypt a tenant's DEK under the system-level tenant master key, for
    storage in tenants.dek_wrapped."""
    master_key = Config.get_tenant_master_key()
    return Fernet(master_key).encrypt(dek)


def unwrap_tenant_dek(wrapped_dek) -> bytes:
    """Decrypt tenants.dek_wrapped back to the raw DEK. Raises InvalidToken
    only if the master key file itself was replaced/corrupted — this is a
    server-side operation, not a per-login check."""
    master_key = Config.get_tenant_master_key()
    return Fernet(master_key).decrypt(bytes(wrapped_dek))


def encrypt_value(value, key: bytes):
    """Encrypt a plaintext string with a tenant's (unwrapped) DEK. Returns bytes for BLOB storage."""
    if value is None or value == "":
        return None
    return Fernet(key).encrypt(str(value).encode("utf-8"))


def decrypt_value(ciphertext, key: bytes):
    """Decrypt Fernet ciphertext back to a plaintext string using a tenant's DEK.

    Returns None if ciphertext is None. Raises ValueError if the key is
    wrong or the data is corrupt — callers should catch this and show a
    "cannot decrypt" message rather than crash the page.
    """
    if ciphertext is None:
        return None
    try:
        return Fernet(key).decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt field — wrong key or corrupt data") from exc


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


salt_to_text = _b64
text_to_salt = _unb64
