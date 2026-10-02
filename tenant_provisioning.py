"""
Shared "create a brand-new tenant + its first Tenant Admin" logic.

Two entry points call this:
  * The self-service /setup wizard (auth/routes.py) -- only reachable before
    any real tenant exists (a manual fallback for an empty database).
  * The SystemAdmin Tenant Management console (blueprints/tenants_admin.py)
    -- the normal way new tour operators are added to TMS.

Kept as one function so the two can never drift apart on what
"provisioning a tenant" means: a fresh wrapped encryption key (DEK), the
next 8-digit account number, the first TenantAdmin user, and the tenant's
own copy of the lookup tables, and its copy of the platform catalogs.
"""
import re
import secrets


def slugify_tenant_code(tenant_name: str) -> str:
    """'Ma Vie Tours' -> 'MA_VIE_TOURS'. The tenant's short internal code
    (exports, future subdomains) -- never shown as "the" name to end users."""
    code = re.sub(r"[^A-Za-z0-9]+", "_", tenant_name.strip()).strip("_").upper()
    return code or secrets.token_hex(4).upper()


def _unique_tenant_code(db, base: str) -> str:
    code, n = base, 2
    while db.execute("SELECT 1 FROM tenants WHERE tenant_code = ?", (code,)).fetchone():
        code = f"{base}_{n}"
        n += 1
    return code


def provision_tenant(db, tenant_name: str, username: str, display_name: str, password: str,
                     tenant_code: str = None, must_change_password: bool = False,
                     website_domain: str = None):
    """Creates the tenant, its first TenantAdmin and its lookup tables.
    Returns (tenant_id, seed_phrase). The recovery seed phrase is generated
    fresh each time; showing it once and discarding it is the caller's job.

    must_change_password=True is for an admin console where someone ELSE
    chose this password and hands it over (Tenant Management); /setup's
    caller picked their own, so it defaults to False.

    Does not validate its inputs -- both callers do that first, with error
    messages suited to their own screen."""
    from db import next_account_number
    from security import crypto
    from security.passwords import hash_password
    from security.wordlist import generate_seed_phrase, hash_phrase

    dek_wrapped = crypto.wrap_tenant_dek(crypto.new_tenant_dek())
    tenant_code = _unique_tenant_code(db, tenant_code or slugify_tenant_code(tenant_name))
    account_number = next_account_number(db)

    cur = db.execute(
        "INSERT INTO tenants (tenant_code, tenant_name, dek_wrapped, account_number, website_domain) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_code, tenant_name, dek_wrapped, account_number, website_domain or None),
    )
    tenant_id = cur.lastrowid

    seed_phrase_value = generate_seed_phrase()
    db.execute(
        """INSERT INTO users (tenant_id, username, display_name, password_hash, role, recovery_seed_hash,
                              must_change_password)
           VALUES (?, ?, ?, ?, 'TenantAdmin', ?, ?)""",
        (tenant_id, username, display_name, hash_password(password), hash_phrase(seed_phrase_value),
         1 if must_change_password else 0),
    )
    db.commit()

    from seed_data import seed_lookup_tables
    seed_lookup_tables(db, tenant_id)
    db.commit()

    # Start the tenant with the platform catalogs (POIs, hotels, restaurants)
    # and keep it in sync from then on -- see catalog_sync.py.
    import catalog_sync
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenant_catalog_links'").fetchone():
        catalog_sync.apply(db, tenant_id, auto=True)

    return tenant_id, seed_phrase_value
