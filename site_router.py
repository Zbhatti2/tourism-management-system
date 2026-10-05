"""
A tenant's own domain serves its public website (Zeb, Oct 2026: "a website
hosted by TMS on the tenant's own domain www.mavietours.com").

The public site lives at /site/<tenant code>/... (blueprints/site.py), which
also works as a preview on the TMS address. This WSGI middleware looks at the
Host of each request: when it is a tenant's website domain (tenants.
website_domain, with or without www., site switched on), the path is mapped
onto that tenant's site -- "/" becomes "/site/mavie/", "/tours/x" becomes
"/site/mavie/tours/x" -- and nothing else of TMS (login, admin screens) can be
reached from that domain. /static/ is shared.

The domain list is read from the database at most once a minute.
"""
import sqlite3
import threading
import time

REFRESH_SECONDS = 60
ENVIRON_KEY = "tms.site_code"


class DomainRouter:
    def __init__(self, wsgi_app, db_path):
        self.wsgi_app = wsgi_app
        self.db_path = db_path
        self._map, self._loaded = {}, 0.0
        self._lock = threading.Lock()

    def _domains(self):
        now = time.time()
        if now - self._loaded > REFRESH_SECONDS:
            with self._lock:
                if now - self._loaded > REFRESH_SECONDS:
                    try:
                        import website
                        con = sqlite3.connect(self.db_path, timeout=5)
                        try:
                            self._map = website.domain_map(con)
                        finally:
                            con.close()
                    except Exception:
                        pass  # no table yet (first start) -- try again later
                    self._loaded = now
        return self._map

    def refresh(self):
        self._loaded = 0.0

    def __call__(self, environ, start_response):
        host = (environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or "").split(",")[0].strip()
        host = host.lower().split(":")[0]
        code = self._domains().get(host) if host else None
        if code:
            path = environ.get("PATH_INFO") or "/"
            environ[ENVIRON_KEY] = code
            if not path.startswith("/static/") and not path.startswith(f"/site/{code}/"):
                environ["PATH_INFO"] = f"/site/{code}" + (path if path.startswith("/") else "/" + path)
        return self.wsgi_app(environ, start_response)


def init_app(app):
    from config import Config
    router = DomainRouter(app.wsgi_app, Config.DATABASE_PATH)
    app.wsgi_app = router
    app.extensions["site_router"] = router
