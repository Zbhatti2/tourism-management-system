"""
Central configuration for the Tourism Management System.

Phase 1.1: multi-tenant, multi-user, server-hosted (even though Phase 1 runs
as a single local Flask process). See schema.sql MODULE T and
security/crypto.py for how authentication (per-user) and field encryption
(per-tenant) are split.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Load a real ".env" file from the App folder (same directory as this file),
# if one exists -- this is how the AI Agents module's ANTHROPIC_API_KEY
# below gets set. See .env.example for the template; ".env" itself is
# gitignored and never committed. python-dotenv is a soft dependency: if
# it isn't installed yet, .env just won't be picked up automatically (an
# OS-level environment variable would still work) -- nothing else in TMS
# depends on it, so the rest of the app runs fine either way.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
EXPORTS_DIR = INSTANCE_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = INSTANCE_DIR / "uploads" / "contacts"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
EMPLOYEE_UPLOADS_DIR = INSTANCE_DIR / "uploads" / "employees"
EMPLOYEE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
BACKUPS_DIR = INSTANCE_DIR / "backups"
BACKUPS_DIR.mkdir(exist_ok=True)


class Config:
    # Flask's own signing key (session cookie, CSRF token signing). Generated
    # once and persisted to instance/secret_key so it survives restarts;
    # NOT the same thing as the field-encryption key(s) below.
    SECRET_KEY_PATH = INSTANCE_DIR / "secret_key"

    @staticmethod
    def get_secret_key() -> bytes:
        if Config.SECRET_KEY_PATH.exists():
            return Config.SECRET_KEY_PATH.read_bytes()
        key = os.urandom(32)
        Config.SECRET_KEY_PATH.write_bytes(key)
        return key

    # System-level master key used ONLY to wrap/unwrap each tenant's field-
    # encryption DEK (tenants.dek_wrapped). Deliberately a separate file from
    # SECRET_KEY_PATH above — different purpose, different blast radius if
    # ever rotated or leaked. Whoever holds this file (the server) can read
    # every tenant's encrypted fields, which is the correct trust boundary
    # for a server-hosted multi-user app (see schema.sql MODULE T).
    TENANT_MASTER_KEY_PATH = INSTANCE_DIR / "tenant_master.key"

    @staticmethod
    def get_tenant_master_key() -> bytes:
        from cryptography.fernet import Fernet
        if Config.TENANT_MASTER_KEY_PATH.exists():
            return Config.TENANT_MASTER_KEY_PATH.read_bytes()
        key = Fernet.generate_key()
        Config.TENANT_MASTER_KEY_PATH.write_bytes(key)
        return key

    DATABASE_PATH = str(INSTANCE_DIR / "tms.db")
    SCHEMA_PATH = str(BASE_DIR / "schema.sql")
    EXPORTS_DIR = str(EXPORTS_DIR)
    UPLOADS_DIR = str(UPLOADS_DIR)
    EMPLOYEE_UPLOADS_DIR = str(EMPLOYEE_UPLOADS_DIR)
    BACKUPS_DIR = str(BACKUPS_DIR)
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB — generous for a profile photo

    # scrypt KDF parameters used by security/passwords.py for user password
    # hashing (werkzeug's scrypt method). n is the memory/CPU cost factor.
    SCRYPT_N = 2 ** 14
    SCRYPT_R = 8
    SCRYPT_P = 1

    # Session inactivity timeout (minutes) — after this, the in-memory
    # tenant encryption key is dropped from this session and the user must
    # log in again.
    SESSION_TIMEOUT_MINUTES = 30

    # AI Agents extraction (blueprints/ai_agents.py's "Run Agent" button,
    # ai_extraction.py's actual model call) -- read from .env, never
    # hardcoded. ANTHROPIC_API_KEY unset means "Run Agent" explains itself
    # and declines rather than failing obscurely; manual review-item entry
    # keeps working either way, since it never touches this. ANTHROPIC_MODEL
    # defaults to this session's own model, but Anthropic's model lineup
    # moves — if extraction calls fail with a model-not-found error, check
    # https://docs.claude.com/en/docs/about-claude/models and override it
    # in .env.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    # The AI Image Collector (image_collector.py) reviews and captions photos
    # with this cheaper vision model; the web search step uses ANTHROPIC_MODEL.
    ANTHROPIC_VISION_MODEL = os.environ.get("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5")

    # Whether the session cookie requires HTTPS. Must stay False for local
    # use (Start_TMS.bat serves plain http://127.0.0.1, where a Secure cookie
    # is dropped and login silently fails). Set the environment variable
    # SESSION_COOKIE_SECURE=true on the server (Coolify), where TMS is only
    # reached over HTTPS.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes")
