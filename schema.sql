-- ============================================================================
-- Tourism Management System — Phase 1.1
-- SQLite DDL. Data model adapted from PIMS (Personal Information Management
-- System) Phase 1, modified for multi-tenancy and multi-user login per the
-- Tourism Management System Phase 1.1 project brief.
--
-- Conventions carried over from PIMS, unchanged:
--   * INTEGER PRIMARY KEY AUTOINCREMENT  -> stable, never-reused surrogate keys
--   * created_at / updated_at            -> bookkeeping timestamps (ISO8601 text, UTC)
--   * "current" tables hold only the active value(s); "_history" tables receive
--     a snapshot row every time a tracked value changes
--   * lookup tables all share the same shape: id, tenant_id, code, label,
--     description, sort_order, is_active
--   * sensitive columns (marked ENCRYPTED) store a Fernet ciphertext, not
--     plaintext — encryption/decryption happens in the application layer (see
--     security/crypto.py), never in SQL
--
-- New conventions for multi-tenancy (see MODULE T below for the full
-- rationale):
--   * Every tenant-owned table carries its own `tenant_id` column (rather
--     than relying solely on joins) so any query can filter with a plain
--     `WHERE tenant_id = ?` — simple and hard to get wrong in application code.
--   * Five lookup tables stay GLOBAL (no tenant_id) because they hold fixed,
--     real-world geography facts every tenant shares: `regions`,
--     `countries`, `states`, `cities`, and `country_phone_codes`. Every
--     other lookup table is tenant-scoped so each tour operator can
--     maintain its own picklists via Table Maintenance without affecting
--     other tenants. The four geography ones have their own admin screen
--     (Geography Maintenance) instead, since editing one affects every
--     tenant, not just the editor's own.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
-- MODULE T — TENANTS & USERS (multi-tenant, multi-user login)
--
-- PIMS (single-user) tied its one master password directly to unwrapping the
-- field-encryption DEK (Data Encryption Key) — a clever trick for one person
-- on one machine, but it does not extend to "N users who all need to read
-- the same tenant's data." For this system, authentication and field
-- encryption are deliberately split:
--
--   * users.password_hash — a standard salted hash (werkzeug/scrypt via
--     security/passwords.py), checked normally at login. Any user of a
--     tenant logs in with their own password; nothing about the tenant's
--     encrypted data depends on which password was used.
--   * tenants.dek_wrapped — each tenant gets its own random DEK, used to
--     encrypt that tenant's sensitive columns (the same kinds of fields PIMS
--     encrypted: subscription passwords, account numbers/CVV/PIN, license
--     serials — see security/crypto.py). The DEK is wrapped under a single
--     system-level master key (instance/tenant_master.key — see config.py),
--     and unwrapped by the server itself once a user's password check
--     succeeds. It is never derived from any user's password.
--
-- This still protects a stolen database file (useless without the master
-- key file, which lives outside the database) and still isolates one
-- tenant's encrypted data from another, while letting any authenticated
-- user of a tenant — and a Tenant Admin resetting a teammate's password —
-- work normally without re-encrypting anything.
--
-- Roles:
--   * SystemAdmin — provisions new tenants, runs whole-database backups.
--     Not scoped to any one tenant (tenant_id is NULL).
--   * TenantAdmin — manages users and data within their own tenant.
--   * User        — regular day-to-day user within their own tenant.
-- ============================================================================

CREATE TABLE tenants (
    tenant_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_code     TEXT NOT NULL UNIQUE,    -- short slug, e.g. 'HERITAGE' — internal identifier (exports, future subdomains), not shown as "the" name to end users
    tenant_name     TEXT NOT NULL,           -- display name, e.g. 'Heritage Tours'
    status          TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Suspended')),
    dek_wrapped     BLOB NOT NULL,           -- Fernet(system tenant-master-key).encrypt(tenant DEK) — see security/crypto.py + config.get_tenant_master_key()
    data_retention_days INTEGER DEFAULT NULL, -- days from a record's date of entry (created_at) until purge-eligible; NULL = indefinite (never auto-purge). Per-tenant: each tour operator sets its own policy.
    last_purge_at   TEXT,                    -- last time this tenant's purge check actually ran (once/day, checked at login), regardless of whether anything was purged
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE users (
    user_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER REFERENCES tenants(tenant_id),   -- NULL only for SystemAdmin (see CHECK below)
    username        TEXT NOT NULL UNIQUE,    -- globally unique login id, so login needs no tenant picker
    display_name    TEXT NOT NULL,
    email           TEXT,
    password_hash   TEXT NOT NULL,           -- werkzeug/scrypt password hash — standard auth, independent of field encryption (see MODULE T note above)
    role            TEXT NOT NULL DEFAULT 'User' CHECK (role IN ('SystemAdmin','TenantAdmin','User')),
    recovery_seed_hash TEXT,                 -- SHA-256 of this user's normalized recovery seed phrase, for self-service "forgot password"
    is_active       INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    last_login_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ( (role = 'SystemAdmin' AND tenant_id IS NULL) OR (role IN ('TenantAdmin','User') AND tenant_id IS NOT NULL) )
);
CREATE INDEX idx_users_tenant ON users(tenant_id);

-- ============================================================================
-- MODULE E — SYSTEM TABLES (lookup / reference data)
-- ============================================================================

-- GLOBAL — shared across every tenant (real-world geography, not tenant opinion).
-- Top tier of the geography hierarchy: Region -> Country -> Province/State -> City.
CREATE TABLE regions (
    region_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE,            -- e.g. 'ASIA'
    label           TEXT NOT NULL,          -- e.g. 'Asia'
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);

-- GLOBAL — shared across every tenant (real-world geography, not tenant opinion).
CREATE TABLE countries (
    country_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id       INTEGER REFERENCES regions(region_id),
    code            TEXT UNIQUE,            -- ISO 3166-1 alpha-2, e.g. 'US'
    label           TEXT NOT NULL,          -- display name, e.g. 'United States'
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_countries_region ON countries(region_id);

-- GLOBAL. The generic Province/State tier. `label` is always the full name
-- (e.g. 'New York', never just 'NY'), so display is consistent everywhere —
-- countries whose provinces have no standard code (Pakistan, India, ...)
-- simply leave `code` NULL.
CREATE TABLE states (
    state_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id      INTEGER NOT NULL REFERENCES countries(country_id),
    code            TEXT,                   -- e.g. 'NY'; NULL where the country's provinces have no code
    label           TEXT NOT NULL,          -- e.g. 'New York', 'Punjab'
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_states_country ON states(country_id);
-- Partial (code can be NULL — e.g. Pakistan's provinces): prevents the same
-- province code being seeded twice under one country while still allowing
-- any number of NULL-code rows (those are deduplicated by label instead,
-- in application code — see seed_data._seed_states).
CREATE UNIQUE INDEX idx_states_country_code ON states(country_id, code) WHERE code IS NOT NULL;

-- GLOBAL. Bottom tier of the geography hierarchy.
CREATE TABLE cities (
    city_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id        INTEGER NOT NULL REFERENCES states(state_id),
    label           TEXT NOT NULL,          -- e.g. 'Lahore'
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_cities_state ON cities(state_id);
CREATE UNIQUE INDEX idx_cities_state_label ON cities(state_id, label);

-- GLOBAL.
CREATE TABLE country_phone_codes (
    country_phone_code_id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id      INTEGER NOT NULL REFERENCES countries(country_id),
    calling_code    TEXT NOT NULL,          -- e.g. '+1'
    label           TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_country_phone_codes_country ON country_phone_codes(country_id);

-- Tenant-scoped lookup tables below: each tenant maintains its own list via
-- Table Maintenance. `code` uniqueness is per-tenant, not global.

CREATE TABLE contact_categories (
    contact_category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_contact_categories_tenant ON contact_categories(tenant_id);

CREATE TABLE contact_titles (
    contact_title_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_contact_titles_tenant ON contact_titles(tenant_id);

CREATE TABLE contact_suffixes (
    contact_suffix_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_contact_suffixes_tenant ON contact_suffixes(tenant_id);

CREATE TABLE professions (
    profession_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_professions_tenant ON professions(tenant_id);

CREATE TABLE contact_contexts (
    contact_context_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_contact_contexts_tenant ON contact_contexts(tenant_id);

CREATE TABLE organization_types (
    organization_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_organization_types_tenant ON organization_types(tenant_id);

-- Lookup for points_of_interest.poi_type_id (see MODULE B below).
CREATE TABLE poi_types (
    poi_type_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_poi_types_tenant ON poi_types(tenant_id);

CREATE TABLE knowledge_domains (
    knowledge_domain_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_knowledge_domains_tenant ON knowledge_domains(tenant_id);

-- One level of nesting under knowledge_domains (e.g. domain "Technology" ->
-- sub-domains "AI/ML", "Cybersecurity"). Added via Table Maintenance.
CREATE TABLE knowledge_subdomains (
    knowledge_subdomain_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    knowledge_domain_id INTEGER NOT NULL REFERENCES knowledge_domains(knowledge_domain_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_knowledge_subdomains_tenant ON knowledge_subdomains(tenant_id);
CREATE INDEX idx_knowledge_subdomains_domain ON knowledge_subdomains(knowledge_domain_id);

CREATE TABLE content_types (
    content_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_content_types_tenant ON content_types(tenant_id);

-- One level of nesting under content_types (e.g. type "Article" -> sub-types
-- "Blog Post", "White Paper"). Added via Table Maintenance.
CREATE TABLE content_subtypes (
    content_subtype_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_type_id INTEGER NOT NULL REFERENCES content_types(content_type_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_content_subtypes_tenant ON content_subtypes(tenant_id);
CREATE INDEX idx_content_subtypes_type ON content_subtypes(content_type_id);

-- Lookup for supplier_addresses.address_type_id (see MODULE C below) — a
-- supplier's several address locations (Main Office, Billing, ...) are
-- distinguished by this, the same way phone_type distinguishes a supplier
-- contact's several phone numbers below. Named supplier_address_types (not
-- just address_types) specifically to keep it distinct from
-- organization_address_types (see MODULE A above) — Organizations and
-- Suppliers keep separate address-type lookups, per the standing rule that
-- the two modules share no type/sub-type/address/contact/phone table.
CREATE TABLE supplier_address_types (
    address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_supplier_address_types_tenant ON supplier_address_types(tenant_id);

-- Lookup for organization_addresses.address_type_id (see MODULE A below) —
-- an organization's several address locations (Mailing Address, Physical
-- Address, ...) are distinguished by this. Deliberately separate from
-- supplier_address_types above, same reasoning.
CREATE TABLE organization_address_types (
    address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_organization_address_types_tenant ON organization_address_types(tenant_id);

-- Lookup for organization_phones.phone_type_id (see MODULE A below) — an
-- organization's several phone numbers (Office, Mobile, Fax, ...) are
-- distinguished by this. Deliberately separate from Suppliers' phone_types
-- (MODULE C below), same reasoning as organization_address_types above.
CREATE TABLE organization_phone_types (
    phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_organization_phone_types_tenant ON organization_phone_types(tenant_id);

-- Lookup for supplier_contact_phones.phone_type_id (see MODULE C below) —
-- Cell / Office / Fax / etc.
CREATE TABLE phone_types (
    phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_phone_types_tenant ON phone_types(tenant_id);

-- Lookup for suppliers.supplier_type_id (see MODULE C below). Deliberately
-- separate from organization_types — Organizations and Suppliers keep
-- distinct type/sub-type tables at every level, per that module's design note.
CREATE TABLE supplier_types (
    supplier_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    template_key    TEXT,                   -- e.g. 'hotel'; NULL = no specialized Template (most types). Per Zeb's "Template linked to the Supplier" request (Sept 2026) -- tells the Supplier view/edit UI which extra Type-specific sections to show (Amenities & Facilities / Rooms for 'hotel'; a future Type gets its own key + sections). See hotel_amenity_options/hotel_room_types below.
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_supplier_types_tenant ON supplier_types(tenant_id);

-- One level of nesting under supplier_types (e.g. type "Hotel" -> sub-types
-- "Five Star", "Motel"). Added via Table Maintenance.
CREATE TABLE supplier_subtypes (
    supplier_subtype_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_type_id INTEGER NOT NULL REFERENCES supplier_types(supplier_type_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_supplier_subtypes_tenant ON supplier_subtypes(tenant_id);
CREATE INDEX idx_supplier_subtypes_type ON supplier_subtypes(supplier_type_id);

-- ============================================================================
-- SHARED — polymorphic addresses (used by Module A contacts)
--
-- Originally also used by the "Accounts" module (owner_type =
-- 'PersonalAccount'); that module was removed in the Points of Interest
-- pivot (see MODULE B below), so 'Contact' is the only owner_type left.
-- Suppliers (MODULE C below) deliberately does NOT use this table — its
-- own supplier_addresses table is kept fully separate from Contacts'/
-- Organizations' address data, per that module's design note.
-- ============================================================================

CREATE TABLE addresses (
    address_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    owner_type      TEXT NOT NULL CHECK (owner_type IN ('Contact', 'Employee', 'ExternalResource')),
    owner_id        INTEGER NOT NULL,       -- FK into contacts.contact_id / employees.employee_id / external_resources.resource_id, per owner_type
    address_type    TEXT,                   -- Home / Business / Billing / On Account, etc.
    street          TEXT,
    unit            TEXT,
    region_id       INTEGER REFERENCES regions(region_id),      -- geography cascade: Region -> Country -> Province/State -> City
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),        -- picked from the states lookup, once country is chosen
    state_province_text TEXT,                                   -- free-text fallback, only used if the country's provinces aren't seeded in the states lookup
    city_id         INTEGER REFERENCES cities(city_id),          -- picked from the cities lookup, once province/state is chosen
    city_text       TEXT,                                        -- free-text fallback, only used if the province's cities aren't seeded in the cities lookup
    postal_code     TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_addresses_tenant ON addresses(tenant_id);
CREATE INDEX idx_addresses_owner ON addresses(owner_type, owner_id);

-- address_id is deliberately NOT a foreign key into addresses(address_id):
-- a "Deleted" history row is written right before the live addresses row is
-- removed (see contacts.py's delete_address), so a hard FK here would make
-- that delete permanently impossible (the just-inserted history row would
-- itself be a dangling reference the moment the live row is gone) — see
-- migrate_fix_history_fk.py for the full story. previous_value already
-- holds a full snapshot, so address_id is just a "this was row #N" label,
-- not a pointer that needs to keep resolving to a live row.
CREATE TABLE addresses_history (
    address_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    address_id      INTEGER NOT NULL,
    owner_type      TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    field_name      TEXT,
    previous_value  TEXT,                   -- JSON snapshot of the superseded address
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_addresses_history_tenant ON addresses_history(tenant_id);
CREATE INDEX idx_addresses_history_address ON addresses_history(address_id);

-- ============================================================================
-- MODULE A — CONTACTS MANAGEMENT
-- ============================================================================

CREATE TABLE organizations (
    organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    organization_name TEXT NOT NULL,
    full_address    TEXT,                   -- superseded by organization_addresses (below) as the structured Street/City/Province/Country record; kept in place (not dropped) as a legacy free-text fallback/audit trail, no longer shown or edited in the UI
    phone           TEXT,                   -- superseded by organization_phones (below), same reasoning as full_address above
    email           TEXT,                   -- superseded by organization_emails (below), same reasoning as full_address above
    website         TEXT,
    organization_type_id INTEGER REFERENCES organization_types(organization_type_id),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organizations_tenant ON organizations(tenant_id);

-- An organization can have more than one address (e.g. Mailing Address,
-- Physical Address — see organization_address_types above), mirroring
-- supplier_addresses (MODULE C below) exactly, but kept fully separate
-- from it — its own table, its own address-type lookup — per the standing
-- rule that Organizations and Suppliers share no address data. Geography
-- fields reuse the same Region -> Country -> Province/State -> City
-- cascade (and city_text/state_province_text free-text fallback) as every
-- other address table in this schema.
CREATE TABLE organization_addresses (
    organization_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
    address_type_id INTEGER REFERENCES organization_address_types(address_type_id),
    street          TEXT,
    unit            TEXT,
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,               -- free-text fallback, same convention as addresses/supplier_addresses
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,                   -- free-text fallback, same convention as addresses/supplier_addresses
    postal_code     TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organization_addresses_tenant ON organization_addresses(tenant_id);
CREATE INDEX idx_organization_addresses_org ON organization_addresses(organization_id);
CREATE INDEX idx_organization_addresses_type ON organization_addresses(address_type_id);

-- An organization can have more than one email address, mirroring
-- contact_emails (MODULE A above) in shape but WITHOUT history tracking —
-- like organization_addresses above, Organizations is master/reference
-- data here, not an owned record with its own audit trail the way a
-- Contact is (spec's Module A design note).
CREATE TABLE organization_emails (
    organization_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
    email_address   TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organization_emails_tenant ON organization_emails(tenant_id);
CREATE INDEX idx_organization_emails_org ON organization_emails(organization_id);

-- An organization can have more than one phone number (Office, Mobile,
-- Fax, ... — see organization_phone_types above), mirroring contact_phones
-- in shape but without history tracking, same reasoning as
-- organization_emails above.
CREATE TABLE organization_phones (
    organization_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
    phone_type_id   INTEGER REFERENCES organization_phone_types(phone_type_id),
    country_code    TEXT,                   -- e.g. '+1'
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organization_phones_tenant ON organization_phones(tenant_id);
CREATE INDEX idx_organization_phones_org ON organization_phones(organization_id);
CREATE INDEX idx_organization_phones_type ON organization_phones(phone_type_id);

-- "Documents Link" feature — an organization can have any number of
-- reference links (a web URL and/or a local document path, each with a
-- short description/notes), mirroring contact_reference_links above
-- exactly. Not the separate MODULE D "Documents Management & Personal
-- Knowledge Base" library (the `content` table and its own link tables) —
-- this is the lighter, per-record link list that lives directly on the
-- organization's own page, same as it does on a Contact's.
CREATE TABLE organization_reference_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    organization_id INTEGER NOT NULL REFERENCES organizations(organization_id),
    url             TEXT,
    document_path   TEXT,                   -- local drive path/name
    description     TEXT,
    notes           TEXT,                   -- brief summary; web URLs are auto-linked on display
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organization_reference_links_tenant ON organization_reference_links(tenant_id);
CREATE INDEX idx_organization_reference_links_org ON organization_reference_links(organization_id);

CREATE TABLE contacts (
    contact_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    title_id        INTEGER REFERENCES contact_titles(contact_title_id),
    full_name       TEXT NOT NULL,
    suffix_id       INTEGER REFERENCES contact_suffixes(contact_suffix_id),
    gender          TEXT,                    -- 'Male' | 'Female' | 'Other' — fixed list, not a lookup table
    file_as         TEXT,
    current_organization_id INTEGER REFERENCES organizations(organization_id),
    current_job_title TEXT,
    profession_id   INTEGER REFERENCES professions(profession_id),
    web_page        TEXT,
    contact_category_id INTEGER REFERENCES contact_categories(contact_category_id),
    context_id      INTEGER REFERENCES contact_contexts(contact_context_id),
    assistant_contact_id INTEGER REFERENCES contacts(contact_id),  -- self-referential: another contact who acts as this one's assistant
    priority_contact INTEGER NOT NULL DEFAULT 0,
    profile_image_path TEXT,
    date_of_birth   TEXT,
    is_deceased     INTEGER NOT NULL DEFAULT 0,
    date_of_death   TEXT,                   -- only meaningful when is_deceased = 1; cleared otherwise
    notes           TEXT,
    knowledge_graph_data TEXT,              -- freeform, ';'-delimited entries (e.g. "Brother of X; Son of Y"); Phase 2: structured triples
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_contacts_tenant ON contacts(tenant_id);
CREATE INDEX idx_contacts_category ON contacts(contact_category_id);
CREATE INDEX idx_contacts_organization ON contacts(current_organization_id);

CREATE TABLE contacts_history (
    contact_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(contact_id),
    field_name      TEXT NOT NULL,          -- 'current_organization_id' or 'current_job_title'
    previous_value  TEXT,
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_contacts_history_tenant ON contacts_history(tenant_id);
CREATE INDEX idx_contacts_history_contact ON contacts_history(contact_id);

CREATE TABLE contact_emails (
    email_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(contact_id),
    email_address   TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_contact_emails_tenant ON contact_emails(tenant_id);
CREATE INDEX idx_contact_emails_contact ON contact_emails(contact_id);

-- email_id is deliberately NOT a foreign key into contact_emails(email_id)
-- — same reason as addresses_history.address_id above (a "Deleted" history
-- row is inserted right before the live row it describes is removed; see
-- migrate_fix_history_fk.py).
CREATE TABLE contact_emails_history (
    email_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    email_id        INTEGER NOT NULL,
    contact_id      INTEGER NOT NULL,
    previous_value  TEXT,
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_contact_emails_history_tenant ON contact_emails_history(tenant_id);

CREATE TABLE contact_phones (
    phone_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(contact_id),
    phone_type      TEXT NOT NULL CHECK (phone_type IN ('Business','Home','Mobile','WhatsApp','Business Fax')),
    country_code    TEXT,                   -- e.g. '+1'
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,                   -- business numbers only
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_contact_phones_tenant ON contact_phones(tenant_id);
CREATE INDEX idx_contact_phones_contact ON contact_phones(contact_id);

-- phone_id is deliberately NOT a foreign key into contact_phones(phone_id)
-- — same reason as addresses_history.address_id above (a "Deleted" history
-- row is inserted right before the live row it describes is removed; see
-- migrate_fix_history_fk.py).
CREATE TABLE contact_phones_history (
    phone_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    phone_id        INTEGER NOT NULL,
    contact_id      INTEGER NOT NULL,
    previous_value  TEXT,                   -- JSON snapshot (type/country/area/number/ext)
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_contact_phones_history_tenant ON contact_phones_history(tenant_id);

CREATE TABLE contact_reference_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(contact_id),
    url             TEXT,
    document_path   TEXT,                   -- local drive path/name
    description     TEXT,
    notes           TEXT,                   -- brief summary; web URLs are auto-linked on display
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_contact_reference_links_tenant ON contact_reference_links(tenant_id);
CREATE INDEX idx_contact_reference_links_contact ON contact_reference_links(contact_id);

-- Archive tables (soft-delete target). Mirror the live shape plus deletion
-- bookkeeping. One per top-level entity that can be deleted directly from
-- the UI; child rows are cascaded into the JSON snapshot rather than
-- archived row-by-row, to keep the purge job simple.
CREATE TABLE contacts_archive (
    contact_id      INTEGER PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    snapshot        TEXT NOT NULL,          -- full JSON snapshot of the contact + children
    deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    purge_eligible_at TEXT
);
CREATE INDEX idx_contacts_archive_tenant ON contacts_archive(tenant_id);

-- ============================================================================
-- MODULE B — POINTS OF INTEREST
--
-- Replaces the removed "Platforms & Subscriptions" and "Accounts" modules
-- (Phase 1.1 pivot toward tourism-attraction data — see
-- Points_Of_Interest_Table.docx for the field spec this table follows).
-- Tenant-scoped, like Organizations/Contacts: each tour operator maintains
-- its own list of attractions. Geography fields reuse the same Region ->
-- Country -> Province/State -> City cascade (and city_text free-text
-- fallback) as the addresses table.
-- ============================================================================

CREATE TABLE points_of_interest (
    poi_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    name            TEXT NOT NULL,          -- "Name of Attraction"
    poi_type_id     INTEGER REFERENCES poi_types(poi_type_id),
    year_established TEXT,
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,               -- free-text fallback, same convention as addresses
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,                   -- free-text fallback, same convention as addresses
    local_location  TEXT,
    phone           TEXT,                   -- freeform "Country+Area+Phone", e.g. '+92-321-4416609'
    fax             TEXT,
    website         TEXT,
    historical_significance TEXT,
    local_contact_id INTEGER REFERENCES contacts(contact_id),
    governing_authority_id INTEGER REFERENCES organizations(organization_id),
    directions      TEXT,
    map_coordinates TEXT,                   -- freeform for now; Phase 2: linked to an actual map
    notes           TEXT,
    links           TEXT,                   -- freeform: links to docs/URLs, one per line
    knowledge_graph_data TEXT,               -- freeform, ';'-delimited entries (e.g. "Sister site of X; Managed by Y"); same convention as contacts.knowledge_graph_data — Phase 2: structured triples
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_poi_tenant ON points_of_interest(tenant_id);
CREATE INDEX idx_poi_type ON points_of_interest(poi_type_id);
CREATE INDEX idx_poi_governing_authority ON points_of_interest(governing_authority_id);
CREATE INDEX idx_poi_local_contact ON points_of_interest(local_contact_id);

-- ============================================================================
-- MODULE C — SUPPLIERS MANAGEMENT
--
-- Vendors a tour operator books through (hotels, transport, restaurants,
-- ...) — deliberately kept fully separate from Organizations at every
-- level, per the request: its own type/sub-type tables (supplier_types /
-- supplier_subtypes, not organization_types), its own addresses
-- (supplier_addresses, not the Module A `addresses` table used by
-- Contacts), its own contacts (supplier_contacts, not the global
-- `contacts` table), and its own phone numbers (supplier_contact_phones,
-- not `contact_phones`). No history tracking on any of this (unlike
-- Contacts) — Suppliers is master/reference data, same as Organizations.
--
-- A supplier can have several address locations (e.g. Main Office,
-- Billing — see `address_types`, a new lookup table); a Supplier Contact
-- optionally links to one of that same supplier's addresses, and has its
-- own phone numbers, each tagged with a phone type (Cell / Office / Fax /
-- ... — see `phone_types`). Geography fields on supplier_addresses reuse
-- the same Region -> Country -> Province/State -> City cascade (and
-- city_text free-text fallback) as the addresses table.
-- ============================================================================

-- is_external_resource flags a Supplier row as a non-salaried/contract
-- person (formerly the separate "External Resources" HR record type --
-- see blueprints/hr.py's retirement note) rather than a vendor company:
-- they provide a service on demand and invoice for it just like any other
-- supplier, so they're modeled as one, with extra columns
-- (company_name/tax_id/national_id_number/hourly_rate) that mainly matter
-- for that case but are harmless left blank on an ordinary supplier.
-- company_name covers the case where the contractor bills through a
-- company rather than as themselves (supplier_name is always their own
-- name in that case) -- distinct from supplier_name, which for an
-- ordinary vendor already IS its company name.
--
-- A flagged row is created and edited through its own dedicated form (the
-- "New/Edit External Resource" screens in blueprints/suppliers.py --
-- new_external_resource()/edit_supplier()), NOT the generic New/Edit
-- Supplier form, after an earlier pass briefly let both flows through the
-- same generic form and that proved confusing in practice. It gets ONE
-- billing address (still stored in supplier_addresses -- see below -- just
-- UI-restricted to a single row) rather than supplier_addresses' normal
-- multiple-addresses-with-types model, and multiple phones/emails/
-- reference links of its own (supplier_phones/supplier_emails/
-- supplier_reference_links, below -- mirroring organization_phones/
-- organization_emails/organization_reference_links in shape) rather than
-- going through a separate supplier_contacts "contact person" sub-record
-- the way an ordinary supplier's staff contact does.
CREATE TABLE suppliers (
    supplier_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_name   TEXT NOT NULL,
    supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id),
    supplier_subtype_id INTEGER REFERENCES supplier_subtypes(supplier_subtype_id),  -- must belong to supplier_type_id; enforced in app code
    is_external_resource INTEGER NOT NULL DEFAULT 0,  -- 1 = a contracted person/resource, not a vendor company
    company_name    TEXT,                  -- billing company, mainly relevant when is_external_resource=1 (a contractor may bill through a company rather than as themselves)
    tax_id          TEXT,
    national_id_number TEXT,               -- e.g. CNIC, mainly relevant when is_external_resource=1
    hourly_rate     REAL,                  -- mainly relevant when is_external_resource=1
    web_page        TEXT,
    notes           TEXT,
    knowledge_graph_data TEXT,              -- freeform, ';'-delimited entries; Phase 2: structured triples (same convention as contacts.knowledge_graph_data)
    preference      TEXT CHECK (preference IN ('Primary','Secondary')),  -- per Zeb's request: lets a Top choice and a Secondary choice be marked among many suppliers of the same Type in the same City (e.g. 100+ Hotels in Lahore) -- NULL for every supplier with no preference set, which is the normal/default case
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_suppliers_tenant ON suppliers(tenant_id);
CREATE INDEX idx_suppliers_type ON suppliers(supplier_type_id);
CREATE INDEX idx_suppliers_subtype ON suppliers(supplier_subtype_id);
CREATE INDEX idx_suppliers_is_external_resource ON suppliers(is_external_resource);

CREATE TABLE supplier_addresses (
    supplier_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    address_type_id INTEGER REFERENCES supplier_address_types(address_type_id),
    street          TEXT,
    unit            TEXT,
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,               -- free-text fallback, same convention as addresses
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,                   -- free-text fallback, same convention as addresses
    postal_code     TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_addresses_tenant ON supplier_addresses(tenant_id);
CREATE INDEX idx_supplier_addresses_supplier ON supplier_addresses(supplier_id);
CREATE INDEX idx_supplier_addresses_type ON supplier_addresses(address_type_id);

CREATE TABLE supplier_contacts (
    supplier_contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    name            TEXT NOT NULL,
    title           TEXT,                   -- "Title/position"
    supplier_address_id INTEGER REFERENCES supplier_addresses(supplier_address_id),  -- picked from this same supplier's linked addresses; enforced in app code
    email           TEXT,
    notes           TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_contacts_tenant ON supplier_contacts(tenant_id);
CREATE INDEX idx_supplier_contacts_supplier ON supplier_contacts(supplier_id);
CREATE INDEX idx_supplier_contacts_address ON supplier_contacts(supplier_address_id);

-- A fax number is just a phone number tagged phone_type = 'Fax' here,
-- rather than a separate column on supplier_contacts — see the module note
-- above ("Maintain a separate Phone Table for phone numbers... to identify
-- cell, office, fax etc.").
CREATE TABLE supplier_contact_phones (
    phone_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_contact_id INTEGER NOT NULL REFERENCES supplier_contacts(supplier_contact_id),
    phone_type_id   INTEGER REFERENCES phone_types(phone_type_id),
    country_code    TEXT,
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_contact_phones_tenant ON supplier_contact_phones(tenant_id);
CREATE INDEX idx_supplier_contact_phones_contact ON supplier_contact_phones(supplier_contact_id);
CREATE INDEX idx_supplier_contact_phones_type ON supplier_contact_phones(phone_type_id);

-- A supplier can have more than one email address of its own -- used by the
-- External Resource form/view (is_external_resource=1) so a contractor can
-- list several emails directly on their own record, mirroring
-- organization_emails in shape (no history tracking, same reasoning as
-- that table's comment). Not used by the ordinary Supplier flow, which
-- keeps its single email on the supplier_contacts "contact person"
-- sub-record instead -- this table exists on every supplier regardless of
-- is_external_resource (nothing stops a regular supplier row from using
-- it too), it's just not exposed in that flow's UI.
CREATE TABLE supplier_emails (
    supplier_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    email_address   TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_emails_tenant ON supplier_emails(tenant_id);
CREATE INDEX idx_supplier_emails_supplier ON supplier_emails(supplier_id);

-- A supplier can have more than one phone number of its own (Cell, Office,
-- Fax, ... -- reuses the same phone_types lookup as supplier_contact_phones
-- above). Same External-Resource-form usage note as supplier_emails above.
CREATE TABLE supplier_phones (
    supplier_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    phone_type_id   INTEGER REFERENCES phone_types(phone_type_id),
    country_code    TEXT,
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_phones_tenant ON supplier_phones(tenant_id);
CREATE INDEX idx_supplier_phones_supplier ON supplier_phones(supplier_id);
CREATE INDEX idx_supplier_phones_type ON supplier_phones(phone_type_id);

-- "Documents Link" feature on a supplier's own record -- a web URL and/or a
-- local document path, each with a short description/notes. Mirrors
-- organization_reference_links exactly. Same External-Resource-form usage
-- note as supplier_emails above.
CREATE TABLE supplier_reference_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    url             TEXT,
    document_path   TEXT,                   -- local drive path/name
    description     TEXT,
    notes           TEXT,                   -- brief summary; web URLs are auto-linked on display
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_reference_links_tenant ON supplier_reference_links(tenant_id);
CREATE INDEX idx_supplier_reference_links_supplier ON supplier_reference_links(supplier_id);

-- ============================================================================
-- MODULE D — DOCUMENTS MANAGEMENT & KNOWLEDGE BASE
-- ============================================================================

CREATE TABLE content (
    content_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_name    TEXT NOT NULL,
    content_type_id INTEGER REFERENCES content_types(content_type_id),
    authors         TEXT,
    description     TEXT,
    notes           TEXT,                    -- brief summary; web URLs are auto-linked on display
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_content_tenant ON content(tenant_id);

CREATE TABLE content_locations (
    location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_id      INTEGER NOT NULL REFERENCES content(content_id),
    location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path')),
    path_or_url     TEXT NOT NULL
);
CREATE INDEX idx_content_locations_tenant ON content_locations(tenant_id);
CREATE INDEX idx_content_locations_content ON content_locations(content_id);

CREATE TABLE content_knowledge_domains (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_id      INTEGER NOT NULL REFERENCES content(content_id),
    knowledge_domain_id INTEGER NOT NULL REFERENCES knowledge_domains(knowledge_domain_id),
    PRIMARY KEY (content_id, knowledge_domain_id)
);
CREATE INDEX idx_content_knowledge_domains_tenant ON content_knowledge_domains(tenant_id);

CREATE TABLE content_keywords (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_id      INTEGER NOT NULL REFERENCES content(content_id),
    term            TEXT NOT NULL,
    PRIMARY KEY (content_id, term)
);
CREATE INDEX idx_content_keywords_tenant ON content_keywords(tenant_id);

CREATE TABLE content_hashtags (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_id      INTEGER NOT NULL REFERENCES content(content_id),
    term            TEXT NOT NULL,
    PRIMARY KEY (content_id, term)
);
CREATE INDEX idx_content_hashtags_tenant ON content_hashtags(tenant_id);

-- "Contacts Link" on the Content form — lets a content item reference an
-- existing Contact, a plain URL, or both, optionally tagged with a Link Type
-- (e.g. "Author", "Reviewed by"). Same list/add/remove pattern as
-- contact_reference_links, but pointed the other way (from Content).
CREATE TABLE content_link_types (
    content_link_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_content_link_types_tenant ON content_link_types(tenant_id);

CREATE TABLE content_contact_links (
    content_contact_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    content_id      INTEGER NOT NULL REFERENCES content(content_id),
    contact_id      INTEGER REFERENCES contacts(contact_id),
    url             TEXT,
    link_type_id    INTEGER REFERENCES content_link_types(content_link_type_id),
    description     TEXT,
    notes           TEXT,                    -- brief summary; web URLs are auto-linked on display
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (contact_id IS NOT NULL OR url IS NOT NULL)
);
CREATE INDEX idx_content_contact_links_tenant ON content_contact_links(tenant_id);
CREATE INDEX idx_content_contact_links_content ON content_contact_links(content_id);
CREATE INDEX idx_content_contact_links_contact ON content_contact_links(contact_id);
CREATE INDEX idx_content_contact_links_type ON content_contact_links(link_type_id);

-- ============================================================================
-- MODULE G — ORGANIZATION INTELLIGENCE
-- ============================================================================
--
-- A running, append-only journal of freeform operational knowledge that
-- staff of the orchestrating organization (this tenant) enter over time —
-- things picked up in the field that don't belong on any one Organization/
-- Supplier/POI record (two spellings of the same place meaning the same
-- thing, a hotel's pet or childcare policy, a route's drive time in bad
-- weather, and so on). No history/versioning table, unlike Contacts —
-- superseding old knowledge isn't done by editing or deleting a prior
-- entry, it's done by simply adding a newer-dated one; entry_date and
-- entered_by_name together are what establish an entry's currency, and the
-- Intelligence screen lists newest entry_date first so the most current
-- information on a topic surfaces on its own without any entry needing to
-- reference or replace another.
CREATE TABLE organization_intelligence (
    intelligence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    entry_date      TEXT NOT NULL,            -- the date this knowledge is current/was entered as of — not always today (a backfilled note can predate created_at)
    entered_by_user_id INTEGER REFERENCES users(user_id),  -- who entered it (the "Source of Entry"); left in place if that user account is later deactivated
    entered_by_name TEXT NOT NULL,            -- display name captured at entry time, so the byline survives even if the user's own display_name later changes
    note            TEXT NOT NULL,            -- the freeform intelligence text itself; web URLs are auto-linked on display
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_organization_intelligence_tenant ON organization_intelligence(tenant_id);
CREATE INDEX idx_organization_intelligence_date ON organization_intelligence(entry_date);

-- ============================================================================
-- MODULE H — HUMAN RESOURCES
-- ============================================================================
--
-- Internal (salaried Employees) and External (non-salaried/contract
-- Resources) human resources, plus one Roles lookup shared by both — a
-- person (employee or external resource) can hold more than one role at
-- once, e.g. Tour Guide + Data Entry. Host Organization (below) is a
-- related but separate concept: the ONE record per tenant representing the
-- tenant's own business (never an Organizations/Suppliers row — those are
-- both for outside parties) — its own office/location address list is
-- where an Employee's Office Work Address is picked from. Host
-- Organization data is managed from System Management, not the Human
-- Resources screen, since it's tenant infrastructure rather than a person
-- record, but it's defined in this module block since Employees depend on
-- it directly.
--
-- Employees and External Resources both get the same History + Archive +
-- Purge/Retention treatment as Contacts (MODULE A) — see contacts_history/
-- contacts_archive there and system_mgmt.py's _purge_eligible() — since HR
-- data (salary, department, job title, hourly rate) is exactly the kind of
-- record worth a change trail and a retention policy, unlike
-- Organizations/Suppliers, which keep no history at all.

-- ---------------------------------------------------------------- host organization

-- Exactly one row per tenant (UNIQUE(tenant_id) below enforces this) —
-- represents the tenant's OWN company, not an outside party. Managed from
-- System Management's "Host Organization" box, auto-created for every
-- tenant (seed_data.seed_host_organization()) so there's always exactly
-- one row to edit, never a blank state to provision manually.
CREATE TABLE host_organizations (
    host_organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL UNIQUE REFERENCES tenants(tenant_id),
    organization_name TEXT NOT NULL,
    website         TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One combined address-type lookup for Host Organization addresses (spec
-- decision: a single list, not separate "Corporate" vs "Location" tables —
-- "Head Office" is just one of the values in this list, same as e.g.
-- "Branch Office"). is_head_office flags the ONE entry that represents
-- Head Office; host_addresses enforces, in application code (blueprints/
-- system_mgmt.py), the hard rule that at most one address can carry that
-- entry's type at a time — not a DB constraint, matching how every other
-- "only one primary/flagged" rule in this schema is enforced.
CREATE TABLE host_address_types (
    address_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_head_office  INTEGER NOT NULL DEFAULT 0,   -- exactly one row per tenant should carry this flag
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_host_address_types_tenant ON host_address_types(tenant_id);

-- The tenant's own office/location addresses — one combined list (spec
-- decision, see host_address_types above). This is what an Employee's
-- Office Work Address (employees.host_address_id) is picked from, and
-- where a Host Phone (below) can optionally be tied to a specific office.
CREATE TABLE host_addresses (
    host_address_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    host_organization_id INTEGER NOT NULL REFERENCES host_organizations(host_organization_id),
    address_type_id INTEGER REFERENCES host_address_types(address_type_id),
    street          TEXT,
    unit            TEXT,
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,
    postal_code     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_host_addresses_tenant ON host_addresses(tenant_id);
CREATE INDEX idx_host_addresses_host_org ON host_addresses(host_organization_id);
CREATE INDEX idx_host_addresses_type ON host_addresses(address_type_id);

CREATE TABLE host_phone_types (
    phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_host_phone_types_tenant ON host_phone_types(tenant_id);

-- Optionally tied to one specific office (host_address_id) — e.g. a Fax
-- number that belongs to a particular branch, needed so an Employee's
-- business card (see employees.host_address_id) can pull the right
-- office's Fax number rather than one single tenant-wide number. A phone
-- left with host_address_id NULL is a general/head-office-level number not
-- tied to any specific address.
CREATE TABLE host_phones (
    host_phone_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    host_organization_id INTEGER NOT NULL REFERENCES host_organizations(host_organization_id),
    host_address_id INTEGER REFERENCES host_addresses(host_address_id),
    phone_type_id   INTEGER REFERENCES host_phone_types(phone_type_id),
    country_code    TEXT,
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_host_phones_tenant ON host_phones(tenant_id);
CREATE INDEX idx_host_phones_host_org ON host_phones(host_organization_id);
CREATE INDEX idx_host_phones_address ON host_phones(host_address_id);

-- ---------------------------------------------------------------- internal human resources (employees)

CREATE TABLE genders (
    gender_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_genders_tenant ON genders(tenant_id);

CREATE TABLE employee_types (
    employee_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_employee_types_tenant ON employee_types(tenant_id);

CREATE TABLE departments (
    department_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_departments_tenant ON departments(tenant_id);

CREATE TABLE job_titles (
    job_title_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_job_titles_tenant ON job_titles(tenant_id);

-- One salaried employee. Personal/home address lives in the shared
-- `addresses` table (owner_type='Employee', owner_id=employee_id — see
-- MODULE A above, extended to allow this owner_type): a single address per
-- spec (not the "multiple with a primary" treatment Emails/Phones get
-- below). Emergency contact is four plain fields directly on this row
-- (not its own address-book entry) — it's a single fallback contact, not
-- a record needing its own CRUD/history.
CREATE TABLE employees (
    employee_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    full_name       TEXT NOT NULL,
    gender_id       INTEGER REFERENCES genders(gender_id),
    employee_type_id INTEGER REFERENCES employee_types(employee_type_id),
    department_id   INTEGER REFERENCES departments(department_id),
    job_title_id    INTEGER REFERENCES job_titles(job_title_id),
    job_role        TEXT,                     -- legacy freeform field, no longer on the Employee form (superseded by Primary/Secondary Role, picked from the Roles lookup) — kept so no historical data is lost, but no longer read or written by the UI
    primary_role_id INTEGER REFERENCES hr_roles(role_id),    -- required at the application layer (see hr.py) — every employee must hold a Primary Role from the shared Roles table (MODULE below); each hr_roles row's own `description` column is that role's "description of duties"
    secondary_role_id INTEGER REFERENCES hr_roles(role_id),  -- optional second role from the same Roles table
    manager_id      INTEGER REFERENCES employees(employee_id),  -- "Reports to" — another Employee at this tenant; NULL for someone at the top of the org chart (self-reference guarded against in hr.py, not by the schema)
    date_of_birth   TEXT,
    host_address_id INTEGER REFERENCES host_addresses(host_address_id),  -- Office Work Address, picked from the Host Organization's own location list (see MODULE H header)
    date_of_hire    TEXT,
    monthly_salary  REAL,
    citizenship_country_id INTEGER REFERENCES countries(country_id),
    passport_number TEXT,
    visa_status     TEXT,
    tax_id          TEXT,                     -- Social Security / Tax ID Number
    national_id_number TEXT,                  -- CNIC or equivalent
    profile_image_path TEXT,
    emergency_contact_name TEXT,
    emergency_contact_address TEXT,
    emergency_contact_phone TEXT,
    emergency_contact_email TEXT,
    notes           TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_employees_tenant ON employees(tenant_id);
CREATE INDEX idx_employees_department ON employees(department_id);
CREATE INDEX idx_employees_host_address ON employees(host_address_id);
CREATE INDEX idx_employees_primary_role ON employees(primary_role_id);
CREATE INDEX idx_employees_secondary_role ON employees(secondary_role_id);
CREATE INDEX idx_employees_manager ON employees(manager_id);
CREATE INDEX idx_employees_gender ON employees(gender_id);

-- Multiple, with one marked primary — same treatment as
-- organization_emails/organization_phones (spec decision for Employees
-- specifically; External Resources keep single Phone/Email columns, per
-- the original field list).
CREATE TABLE employee_emails (
    employee_email_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
    email_address   TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_employee_emails_tenant ON employee_emails(tenant_id);
CREATE INDEX idx_employee_emails_employee ON employee_emails(employee_id);

CREATE TABLE employee_phone_types (
    phone_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_employee_phone_types_tenant ON employee_phone_types(tenant_id);

CREATE TABLE employee_phones (
    employee_phone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
    phone_type_id   INTEGER REFERENCES employee_phone_types(phone_type_id),
    country_code    TEXT,
    area_code       TEXT,
    number          TEXT NOT NULL,
    extension       TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_employee_phones_tenant ON employee_phones(tenant_id);
CREATE INDEX idx_employee_phones_employee ON employee_phones(employee_id);
CREATE INDEX idx_employee_phones_type ON employee_phones(phone_type_id);

-- Repeatable Education entries (Degrees, Certificates) — a fixed 2-value
-- enum via CHECK, same convention as contact_phones.phone_type, rather
-- than a full lookup table (no third category was asked for).
CREATE TABLE employee_education (
    education_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
    credential_type TEXT NOT NULL CHECK (credential_type IN ('Degree','Certificate')),
    title           TEXT NOT NULL,            -- e.g. "BSc Computer Science", "PMP Certification"
    institution     TEXT,
    year_completed  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_employee_education_tenant ON employee_education(tenant_id);
CREATE INDEX idx_employee_education_employee ON employee_education(employee_id);

-- "Document Links" — resume, certificate/degree copies, etc. Mirrors
-- organization_reference_links exactly.
CREATE TABLE employee_document_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
    url             TEXT,
    document_path   TEXT,
    description     TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_employee_document_links_tenant ON employee_document_links(tenant_id);
CREATE INDEX idx_employee_document_links_employee ON employee_document_links(employee_id);

-- Change history for mutable, HR-meaningful fields — mirrors
-- contacts_history exactly (see MODULE A). Tracked fields: department_id,
-- job_title_id, employee_type_id, monthly_salary, host_address_id (office
-- reassignment).
CREATE TABLE employees_history (
    employee_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    employee_id     INTEGER NOT NULL REFERENCES employees(employee_id),
    field_name      TEXT NOT NULL,
    previous_value  TEXT,
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_employees_history_tenant ON employees_history(tenant_id);
CREATE INDEX idx_employees_history_employee ON employees_history(employee_id);

-- Soft-delete archive + Purge/Retention — mirrors contacts_archive exactly
-- (same tenants.data_retention_days window, same manual "Run purge now"
-- plus once-a-day-at-login check in system_mgmt.py).
CREATE TABLE employees_archive (
    employee_id     INTEGER PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    snapshot        TEXT NOT NULL,
    deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    purge_eligible_at TEXT
);
CREATE INDEX idx_employees_archive_tenant ON employees_archive(tenant_id);

-- ---------------------------------------------------------------- external human resources (resources)

CREATE TABLE resource_types (
    resource_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_resource_types_tenant ON resource_types(tenant_id);

-- RETIRED (kept, unused): external_resources / external_resources_history /
-- external_resources_archive below, plus the resource_types lookup above,
-- are no longer read or written by the app -- superseded by
-- suppliers.is_external_resource (see MODULE C below and blueprints/hr.py's
-- retirement note). Kept in schema.sql rather than dropped, same "never
-- destroy, just stop using" treatment as employees.job_role -- though these
-- three tables were confirmed empty before the change, so nothing was lost.
--
-- A non-salaried/contract resource. Address is a single row in the shared
-- `addresses` table (owner_type='ExternalResource'), same as Employees.
-- Phone/Email stay single-value plain columns (the "multiple, with a
-- primary" treatment was specifically requested for Employees, not here).
-- organization_id links this resource to the company/firm it works
-- through (e.g. a contractor supplied by an agency) — the same shared
-- `organizations` table Contacts links to via current_organization_id, not
-- a separate Resource-specific lookup; see organizations.py's REFERENCES
-- list for the delete-blocking this creates.
CREATE TABLE external_resources (
    resource_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    full_name       TEXT NOT NULL,
    resource_type_id INTEGER REFERENCES resource_types(resource_type_id),
    organization_id INTEGER REFERENCES organizations(organization_id),
    phone           TEXT,
    email           TEXT,
    tax_id          TEXT,
    national_id_number TEXT,
    hourly_rate     REAL,
    notes           TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_external_resources_tenant ON external_resources(tenant_id);
CREATE INDEX idx_external_resources_type ON external_resources(resource_type_id);
CREATE INDEX idx_external_resources_organization ON external_resources(organization_id);

-- Same History + Archive/Purge treatment as Employees above (the spec's
-- History Log request is applied consistently across the whole HR module,
-- not just Employees). Tracked fields: resource_type_id, hourly_rate.
CREATE TABLE external_resources_history (
    resource_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    resource_id     INTEGER NOT NULL REFERENCES external_resources(resource_id),
    field_name      TEXT NOT NULL,
    previous_value  TEXT,
    superseded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_reason TEXT
);
CREATE INDEX idx_external_resources_history_tenant ON external_resources_history(tenant_id);
CREATE INDEX idx_external_resources_history_resource ON external_resources_history(resource_id);

CREATE TABLE external_resources_archive (
    resource_id     INTEGER PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    snapshot        TEXT NOT NULL,
    deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    purge_eligible_at TEXT
);
CREATE INDEX idx_external_resources_archive_tenant ON external_resources_archive(tenant_id);

-- ---------------------------------------------------------------- roles (shared)

-- One Roles lookup shared by BOTH Employees and External Resources — the
-- one lookup table in this module that is deliberately NOT kept separate
-- per sub-module, since it's explicitly "a Common Table" to both.
CREATE TABLE hr_roles (
    role_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_hr_roles_tenant ON hr_roles(tenant_id);

-- A person (Employee or External Resource) can hold more than one Role at
-- once (spec decision). Polymorphic owner_type/owner_id, same pattern as
-- the shared `addresses` table above.
CREATE TABLE hr_role_assignments (
    assignment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    owner_type      TEXT NOT NULL CHECK (owner_type IN ('Employee','ExternalResource')),
    owner_id        INTEGER NOT NULL,
    role_id         INTEGER NOT NULL REFERENCES hr_roles(role_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, owner_type, owner_id, role_id)
);
CREATE INDEX idx_hr_role_assignments_tenant ON hr_role_assignments(tenant_id);
CREATE INDEX idx_hr_role_assignments_owner ON hr_role_assignments(owner_type, owner_id);
CREATE INDEX idx_hr_role_assignments_role ON hr_role_assignments(role_id);

-- ============================================================================
-- MODULE F — DATA EXCHANGE (IMPORT / EXPORT STAGING)
-- ============================================================================

CREATE TABLE import_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    source_type     TEXT CHECK (source_type IN ('CSV','vCard','JSON')),
    target_module   TEXT NOT NULL,          -- e.g. 'contacts'
    file_name       TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'Staged' CHECK (status IN ('Staged','Validated','Committed','Rejected')),
    row_count       INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0
);
CREATE INDEX idx_import_batches_tenant ON import_batches(tenant_id);

CREATE TABLE import_staging_rows (
    staging_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    batch_id        INTEGER NOT NULL REFERENCES import_batches(batch_id),
    raw_data        TEXT NOT NULL,          -- JSON blob of the source row
    validation_status TEXT NOT NULL DEFAULT 'Pending' CHECK (validation_status IN ('Pending','Valid','Invalid')),
    validation_errors TEXT,
    committed_entity_id INTEGER             -- nullable, filled in after commit
);
CREATE INDEX idx_import_staging_rows_tenant ON import_staging_rows(tenant_id);
CREATE INDEX idx_import_staging_rows_batch ON import_staging_rows(batch_id);

CREATE TABLE export_jobs (
    export_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    module          TEXT NOT NULL,
    format          TEXT CHECK (format IN ('CSV','JSON','vCard','PDF')),
    filters         TEXT,                   -- JSON
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    file_path       TEXT
);
CREATE INDEX idx_export_jobs_tenant ON export_jobs(tenant_id);

-- ============================================================================
-- SYSTEM MANAGEMENT MODULE
-- ============================================================================

CREATE TABLE audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER REFERENCES tenants(tenant_id),  -- NULL for pre-login/system-level events (e.g. failed login before the tenant is known, tenant provisioning by a SystemAdmin)
    user_id         INTEGER REFERENCES users(user_id),      -- NULL for failed logins / system events
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    action          TEXT NOT NULL,          -- Create / Update / Delete / Login / Import / Export / Purge / Archive / ProvisionTenant
    entity_type     TEXT,
    entity_id       INTEGER,
    detail          TEXT
);
CREATE INDEX idx_audit_log_tenant_ts ON audit_log(tenant_id, ts);
CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id);

-- A log row per CSV file produced by "Archive entries older than 1 year" on
-- the Audit Log page. This is a SystemAdmin, whole-database operation (the
-- audit_log table holds every tenant's rows plus system-level ones), the
-- same way `backups` below is.
CREATE TABLE audit_log_archives (
    archive_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    oldest_ts       TEXT,                    -- ts of the oldest row in this archive
    newest_ts       TEXT,                    -- ts of the newest row in this archive (just under the cutoff)
    cutoff_ts       TEXT NOT NULL,            -- the "older than" cutoff used for this run
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_log_archives_created ON audit_log_archives(created_at DESC);

-- A log row per backup file under instance/backups/. Whole-database (every
-- tenant's data lives in the one SQLite file), so this is a SystemAdmin
-- operation, not a per-tenant one. 'Manual' = created from the Backup &
-- Restore page. 'Pre-restore safety' = taken automatically right before a
-- restore overwrites the live database, so a bad restore is itself
-- undoable. 'Restore point' isn't a fresh backup — it's a marker appended
-- to the (now-restored) log recording what the database was just restored
-- from.
CREATE TABLE backups (
    backup_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    table_count     INTEGER,
    total_rows      INTEGER,
    integrity_ok    INTEGER,                -- 1/0 — PRAGMA integrity_check result captured at backup time
    backup_type     TEXT NOT NULL DEFAULT 'Manual' CHECK (backup_type IN ('Manual','Pre-restore safety','Restore point')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);
CREATE INDEX idx_backups_created ON backups(created_at DESC);

-- ============================================================================
-- MODULE I — INVENTORY: SERVICES (Service Coding System)
--
-- Per Service_Coding_System_Schema.md (supplied 2026-09): a nested
-- Category -> Group -> Sub-Group -> Sequence code (e.g. 'TP-MN-MC-0001')
-- that identifies every sellable service across the ten active Categories
-- (Tour Package, Ground Transport, Rail Transport, Airline, Shipping/Boat,
-- Accommodation, Food & Beverages, Guide Services, Entry Tickets/Permits,
-- VISA Processing), plus five retired legacy Categories kept for history
-- (is_active = 0 -- see seed_data.SERVICE_CATEGORIES).
--
-- This is Inventory Management's "Services" sub-module -- the first of its
-- two planned sub-modules, per the request:
--
--   "The first is 'Services' which will have its own table since there
--   are no Quantities On hand and Minimum Order Quantity type
--   requirements. Services inventory is a Services Code and a
--   Description. The services table will primarily be used for planning
--   and formalizing a Tour package."
--
-- Design decisions (see migrate_add_services.py for the full story):
--
--   * service_categories / service_groups / service_subgroups are GLOBAL
--     (no tenant_id) -- same treatment as regions/countries/states/cities
--     above: this is a fixed, shared coding vocabulary, not each tenant's
--     private opinion. The source design doc's Part 2 (per-tenant
--     overrides of validity_status, tenant-added custom Categories) is
--     NOT built here -- there's only one tenant today and the user's own
--     framing of this task was the simpler single-table "Services inventory
--     is a Services Code and a Description," not the full multi-tenant
--     configurability doc. Flagged as a scope decision, easy to extend
--     later (an overrides table alongside these, never mutating them in
--     place) if a second tenant ever needs a genuinely different taxonomy.
--
--   * `services` (the actual catalog a tenant builds) IS tenant-scoped,
--     like every other business table here -- each tour operator keeps
--     its own list of services it sells, and `sequence_number` resets to
--     0001 independently per tenant per Sub-Group bucket (UNIQUE
--     (tenant_id, subgroup_id, sequence_number) below), never a shared
--     global counter.
--
--   * validity_status ('active' / 'deprecated' / 'under_review') on
--     service_subgroups is the mechanism that blocks issuing a code
--     against a combination the design doc identified as invalid (e.g.
--     'GT-BS-LN' -- no commercial operator offers a self-drive coach).
--     Enforced three ways, matching the design doc's defense-in-depth:
--     (1) the New Service form's Sub-Group picker only offers 'active'/
--     'under_review' options (blueprints/services.py's taxonomy-tree
--     endpoint excludes 'deprecated' entirely); (2) the same check runs
--     again in the route handler before the INSERT; (3) the
--     trg_block_deprecated_service_subgroup trigger below is the last
--     line of defense at the database layer itself, catching anything
--     that bypassed the UI or the route (a bulk script, a future API).
-- ============================================================================

-- GLOBAL -- shared coding vocabulary, not tenant opinion (see module note).
CREATE TABLE service_categories (
    category_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code   TEXT NOT NULL UNIQUE,    -- 'TP', 'GT', 'RT', 'AT', 'ST', 'AR', 'FB', 'GS', 'ET', 'VP' (+ 5 retired legacy codes)
    category_name   TEXT NOT NULL,
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,   -- 0 for the 5 retired legacy categories (TC/TB/TR/TA/TS) -- kept for history, never dropped
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- GLOBAL.
CREATE TABLE service_groups (
    group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id     INTEGER NOT NULL REFERENCES service_categories(category_id),
    group_code      TEXT NOT NULL,           -- e.g. 'MN' (Multi-Nation Tour) under TP
    group_name      TEXT NOT NULL,
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (category_id, group_code)
);
CREATE INDEX idx_service_groups_category ON service_groups(category_id);

-- GLOBAL.
CREATE TABLE service_subgroups (
    subgroup_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES service_groups(group_id),
    subgroup_code   TEXT NOT NULL,           -- e.g. 'MC' (Multi-City POIs)
    subgroup_name   TEXT NOT NULL,
    description     TEXT,
    validity_status TEXT NOT NULL DEFAULT 'active' CHECK (validity_status IN ('active','deprecated','under_review')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (group_id, subgroup_code)
);
CREATE INDEX idx_service_subgroups_group ON service_subgroups(group_id);
CREATE INDEX idx_service_subgroups_validity ON service_subgroups(validity_status);

-- Tenant-scoped -- each tour operator's own catalog of sellable services,
-- built against the shared taxonomy above. `service_code` is the
-- human-readable 13-character code (e.g. 'TP-MN-MC-0001'), denormalized
-- here for display/query convenience alongside the normalized subgroup_id
-- + sequence_number pair that actually defines it.
CREATE TABLE services (
    service_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    subgroup_id     INTEGER NOT NULL REFERENCES service_subgroups(subgroup_id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 9999),
    service_code    TEXT NOT NULL,           -- e.g. 'TP-MN-MC-0001' -- denormalized, kept in sync with subgroup_id/sequence_number at write time
    description     TEXT NOT NULL,           -- the human-readable service, e.g. "Multi-Day Tour Leader/Escort, combined Malaysia-Pakistan tour"
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','archived')),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, subgroup_id, sequence_number)
);
CREATE INDEX idx_services_tenant ON services(tenant_id);
CREATE INDEX idx_services_subgroup ON services(subgroup_id);
CREATE UNIQUE INDEX idx_services_tenant_code ON services(tenant_id, service_code);

-- Enforcement trigger (schema doc S9.2 / S9.3 layer 3) -- the database-
-- layer backstop: a Service Code can never be issued against a Sub-Group
-- whose validity_status is 'deprecated', regardless of entry path.
CREATE TRIGGER trg_block_deprecated_service_subgroup
BEFORE INSERT ON services
FOR EACH ROW
WHEN (SELECT validity_status FROM service_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
BEGIN
    SELECT RAISE(ABORT, 'Cannot issue a Service Code for a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
END;

-- Same check on UPDATE, in case a row's subgroup_id is ever changed after
-- creation (the UI does not offer this -- Sub-Group/sequence are treated
-- as immutable once a code is issued, matching the design doc's stability
-- rationale for invoicing -- but the database enforces it either way).
CREATE TRIGGER trg_block_deprecated_service_subgroup_update
BEFORE UPDATE OF subgroup_id ON services
FOR EACH ROW
WHEN (SELECT validity_status FROM service_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
BEGIN
    SELECT RAISE(ABORT, 'Cannot move a Service Code onto a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
END;

-- ============================================================================
-- MODULE J — INVENTORY: PRODUCTS (Product Coding System)
--
-- The second Inventory sub-module, structurally parallel to MODULE I
-- (Services) above but a SEPARATE, INDEPENDENT registry -- built from
-- Claude_Code_Prompt_Products_Module.md's explicit instruction:
--
--     "Do not add rows to service_category/service_group/service_subgroup/
--     service_code. Build parallel tables: product_category, product_
--     group, product_subgroup, product_code. Uniqueness constraints are
--     scoped within this registry only -- a 2-letter code reused between
--     Services and Products ... is expected and harmless, since the two
--     registries never share a uniqueness constraint."
--
-- Same nested Category -> Group -> Sub-Group -> Sequence code shape as
-- Services (e.g. 'BP-CM-FC-0001'), same GLOBAL-taxonomy-plus-tenant-
-- scoped-catalog split, same validity_status/deprecation-trigger
-- mechanism -- but table names, FKs, and every UNIQUE constraint below are
-- fully independent of service_categories/service_groups/service_
-- subgroups/services. A query against product_categories can never
-- implicitly join service_categories (see test_products.py's explicit
-- proof of this).
--
-- Deviations from the source prompt's mental model (a generic Node/
-- Postgres template), kept consistent with how Services was actually
-- built in THIS codebase instead of the prompt's assumed shape:
--   * No separate `product_code` registry table. Services has no
--     separate `service_code` table either -- the code string is
--     denormalized directly onto the tenant-scoped catalog row
--     (products.product_code), computed from subgroup_id + sequence_
--     number, with UNIQUE(tenant_id, subgroup_id, sequence_number) and
--     UNIQUE(tenant_id, product_code) providing the equivalent integrity
--     guarantee a separate registry table + FK would have given.
--   * The master `products` table is tenant-scoped, not global. The
--     prompt's Part 2 doesn't address multi-tenancy at all (a generic
--     single-tenant template), but this app is multi-tenant throughout,
--     and a physical product catalog -- with its own price/cost/stock_
--     quantity -- is exactly the kind of thing where two tour operators
--     legitimately keep separate inventories, the same reasoning that
--     already made `services` tenant-scoped. product_categories/product_
--     groups/product_subgroups (the shared coding vocabulary) remain
--     GLOBAL, same split as Services.
--   * Attribute storage: a normalized product_attributes key-value table,
--     not a JSON/JSONB column (the prompt asked for one or the other,
--     explained before implementing). This codebase has no JSON column
--     anywhere -- every other flexible/multi-value shape (contact_emails,
--     contact_phones, employee_phones, addresses, content_keywords, ...)
--     is a normalized child table, and SQLite's JSON support is function-
--     based (json_extract/json_each) rather than a native indexed type,
--     so a key-value table also gets a real index on attribute_key/value
--     and fits the query patterns (Table Maintenance-style filtering,
--     WHERE-clause search) the rest of this app already uses -- a JSON
--     column would be the first of its kind here and would need those
--     queries rewritten around json_extract() instead.
-- ============================================================================

-- GLOBAL -- shared coding vocabulary, not tenant opinion (see module note).
CREATE TABLE product_categories (
    category_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code   TEXT NOT NULL UNIQUE,    -- 'BP', 'TA', 'BG'
    category_name   TEXT NOT NULL,
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- GLOBAL.
CREATE TABLE product_groups (
    group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id     INTEGER NOT NULL REFERENCES product_categories(category_id),
    group_code      TEXT NOT NULL,           -- e.g. 'CM' (Cosmetics & Color Cosmetics) under BP
    group_name      TEXT NOT NULL,
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (category_id, group_code)
);
CREATE INDEX idx_product_groups_category ON product_groups(category_id);

-- GLOBAL.
CREATE TABLE product_subgroups (
    subgroup_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES product_groups(group_id),
    subgroup_code   TEXT NOT NULL,           -- e.g. 'FC' (Face)
    subgroup_name   TEXT NOT NULL,
    description     TEXT,
    validity_status TEXT NOT NULL DEFAULT 'active' CHECK (validity_status IN ('active','deprecated','under_review')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (group_id, subgroup_code)
);
CREATE INDEX idx_product_subgroups_group ON product_subgroups(group_id);
CREATE INDEX idx_product_subgroups_validity ON product_subgroups(validity_status);

-- Tenant-scoped -- each tour operator's own physical product inventory,
-- built against the shared taxonomy above. `product_code` is the
-- human-readable 13-character code (e.g. 'BP-CM-FC-0001'), denormalized
-- here for display/query convenience alongside the normalized subgroup_id
-- + sequence_number pair that actually defines it (see module note above
-- for why there's no separate product_code registry table).
CREATE TABLE products (
    product_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    subgroup_id     INTEGER NOT NULL REFERENCES product_subgroups(subgroup_id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 9999),
    product_code    TEXT NOT NULL,
    product_name    TEXT NOT NULL,
    description     TEXT,
    sku             TEXT,
    price           NUMERIC,
    cost            NUMERIC,
    currency        TEXT,
    stock_quantity  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','discontinued')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, subgroup_id, sequence_number)
);
CREATE INDEX idx_products_tenant ON products(tenant_id);
CREATE INDEX idx_products_subgroup ON products(subgroup_id);
CREATE UNIQUE INDEX idx_products_tenant_code ON products(tenant_id, product_code);

-- Flexible category-specific attributes (shade/size for cosmetics, capacity
-- for luggage, ...) -- see module note above for the JSON-column-vs-
-- key-value-table decision. Not seeded from TTMS_Products_Seed_Data.csv
-- (that file carries no per-product attribute values, only the taxonomy +
-- product names) -- the mechanism exists for future use, same "build the
-- mechanism even though nothing uses it yet" spirit as the validity_status
-- enforcement trigger below.
CREATE TABLE product_attributes (
    attribute_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    product_id      INTEGER NOT NULL REFERENCES products(product_id),
    attribute_key   TEXT NOT NULL,
    attribute_value TEXT,
    UNIQUE (product_id, attribute_key)
);
CREATE INDEX idx_product_attributes_tenant ON product_attributes(tenant_id);
CREATE INDEX idx_product_attributes_product ON product_attributes(product_id);

-- Same defense-in-depth as Services' trg_block_deprecated_service_
-- subgroup: this taxonomy currently has zero deprecated/under-review
-- combinations (every product_subgroups row seeds as 'active'), but the
-- enforcement mechanism exists now for future additions, per the prompt's
-- explicit instruction not to skip it just because nothing uses it yet.
CREATE TRIGGER trg_block_deprecated_product_subgroup
BEFORE INSERT ON products
FOR EACH ROW
WHEN (SELECT validity_status FROM product_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
BEGIN
    SELECT RAISE(ABORT, 'Cannot issue a Product Code for a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
END;

CREATE TRIGGER trg_block_deprecated_product_subgroup_update
BEFORE UPDATE OF subgroup_id ON products
FOR EACH ROW
WHEN (SELECT validity_status FROM product_subgroups WHERE subgroup_id = NEW.subgroup_id) = 'deprecated'
BEGIN
    SELECT RAISE(ABORT, 'Cannot move a Product Code onto a deprecated Sub-Group: this combination has been identified as invalid and is blocked.');
END;

-- ============================================================================
-- MODULE K — PACKAGE MANAGEMENT & ITINERARY BUILDER
--
-- The first build-out of the "Next-Phase Blueprint" roadmap (published as
-- an Artifact alongside this session) -- the load-bearing gap identified
-- there: nothing downstream (Departures, Bookings, Invoicing) has anything
-- to attach to until a sellable Package template exists. Absorbs what the
-- source planning docs called "Phase 1: Product Design" AND "Phase 2:
-- Costing & Pricing" into one module, because in this schema costing is a
-- property of each package_components row, not a separate phase --
-- products already carry price/cost (MODULE J), so there is no separate
-- costing stage to build.
--
-- Unlike Services/Products (MODULE I/J), Package Management has NO global
-- taxonomy layer -- every table here is tenant-scoped. A package is one
-- operator's own product, assembled from things that ARE shared/coded
-- (Services, Products, Suppliers, Points of Interest, Geography) but the
-- assembly itself is not a coding vocabulary to share across tenants.
--
-- packages.service_id (added after this module's initial build, per Zeb's
-- request) is the one exception: every package is required to originate
-- from an existing Services Inventory row in the 'TP' (Tour Package)
-- Category -- the tenant defines the tour concept there first (e.g.
-- service_code 'TP-SN-MC-0001', description "Boston (USA) to Pakistan
-- (Lahore, Nankana Sahib, Islamabad, HasanAbdal) Gurdwaras Tours"), then
-- turns it into a real, itinerary-bearing Package here. package_code is
-- therefore NOT free-text -- it's copied verbatim from the linked
-- service's service_code at creation time, and package_name is copied
-- from that service's own description. This gives Package Numbers the
-- same Category-Group-Subgroup-Sequence traceability Services already
-- has (a new city's tour becomes the next sequence, e.g. ...-0002),
-- without duplicating the Service Coding System's tables here.
--
-- The six tables here map directly onto the Blueprint's schema sketch:
--   packages              -- the template itself (Phase 1's "day-by-day
--                            skeleton" before it's dated)
--   package_route_stops   -- the Master Route: Country -> City sequence
--   package_days          -- day-by-day skeleton, anchored to a route stop
--   package_day_pois      -- POIs scheduled per day (links straight into
--                            the existing points_of_interest table)
--   package_components    -- the priceable building blocks: wraps an
--                            existing Service or Product (or a one-off
--                            'custom' line) plus which Supplier fulfills
--                            it -- this is where Costing actually lives
--   package_price_tiers   -- per-person price by group-size band
--
-- Departures (dated instances) and Bookings (the sales transaction) are
-- explicitly OUT of scope here -- next phases on the roadmap, once this
-- module exists for them to build on.
-- ============================================================================

CREATE TABLE packages (
    package_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    service_id      INTEGER REFERENCES services(service_id),  -- the 'TP' (Tour Package) category service this package instantiates; package_code/package_name are copied from it at creation
    package_code    TEXT NOT NULL,          -- copied from the linked service's service_code, e.g. 'TP-SN-MC-0001'
    package_name    TEXT NOT NULL,
    package_type    TEXT NOT NULL DEFAULT 'fixed_departure' CHECK (package_type IN ('fixed_departure','custom')),
    duration_days   INTEGER,
    duration_nights INTEGER,
    min_pax         INTEGER,
    max_pax         INTEGER,
    difficulty_rating TEXT,
    minimum_age     INTEGER,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
    description     TEXT,
    inclusions      TEXT,
    exclusions      TEXT,
    base_currency   TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, package_code)
);
CREATE INDEX idx_packages_tenant ON packages(tenant_id);
CREATE INDEX idx_packages_status ON packages(status);
CREATE INDEX idx_packages_service ON packages(service_id);

-- The Master Route -- Country -> City sequence a package travels.
CREATE TABLE package_route_stops (
    stop_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    sequence_number INTEGER NOT NULL,
    region_id       INTEGER REFERENCES regions(region_id),
    country_id      INTEGER REFERENCES countries(country_id),
    state_id        INTEGER REFERENCES states(state_id),
    state_province_text TEXT,               -- free-text fallback, same convention as addresses/POI
    city_id         INTEGER REFERENCES cities(city_id),
    city_text       TEXT,                   -- free-text fallback, same convention as addresses/POI
    nights          INTEGER,
    is_layover      INTEGER NOT NULL DEFAULT 0,  -- a transit/connection stop, not an overnight stay -- masks Nights in favor of layover_hours (see stop_form.html)
    layover_hours   NUMERIC,                -- approximate connection time; only meaningful when is_layover = 1
    is_checkpoint   INTEGER NOT NULL DEFAULT 0,  -- a stop where accommodations need to be arranged for the group -- a planning flag, independent of is_layover (see stop_form.html)
    border_crossing_notes TEXT,             -- handoff point from one country's DMC to the next
    UNIQUE (package_id, sequence_number)
);
CREATE INDEX idx_package_stops_package ON package_route_stops(package_id);
CREATE INDEX idx_package_stops_tenant ON package_route_stops(tenant_id);

-- Day-by-day skeleton, anchored to a route stop.
CREATE TABLE package_days (
    day_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    route_stop_id   INTEGER REFERENCES package_route_stops(stop_id),
    day_number      INTEGER NOT NULL,
    title           TEXT,
    description     TEXT,
    UNIQUE (package_id, day_number)
);
CREATE INDEX idx_package_days_package ON package_days(package_id);
CREATE INDEX idx_package_days_stop ON package_days(route_stop_id);
CREATE INDEX idx_package_days_tenant ON package_days(tenant_id);

-- POIs scheduled per day -- links straight into the existing MODULE B
-- points_of_interest table rather than duplicating attraction data here.
CREATE TABLE package_day_pois (
    day_poi_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    day_id          INTEGER NOT NULL REFERENCES package_days(day_id),
    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
    sequence_number INTEGER NOT NULL,
    visit_notes     TEXT,
    UNIQUE (day_id, poi_id)
);
CREATE INDEX idx_package_day_pois_day ON package_day_pois(day_id);
CREATE INDEX idx_package_day_pois_poi ON package_day_pois(poi_id);
CREATE INDEX idx_package_day_pois_tenant ON package_day_pois(tenant_id);

-- The priceable building blocks -- accommodation, transport, guide, entry
-- fees, meals. Wraps an existing Service or Product row (component_type
-- pins which FK applies), or stands alone as a one-off 'custom' line, plus
-- which Supplier fulfills it. This is where Costing lives -- a property of
-- the component, not a separate phase (see module note above).
CREATE TABLE package_components (
    component_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    route_stop_id   INTEGER REFERENCES package_route_stops(stop_id),  -- nullable: whole-trip items (e.g. tour leader) aren't tied to one stop
    day_id          INTEGER REFERENCES package_days(day_id),  -- nullable: ties a component to one specific Day rather than (or on top of) a whole Route Stop -- lets Costing be itemized per Day (e.g. one night's hotel room), per Zeb's Day-by-Day "Hotel/Room" and "Other Service" sub-parts request. Independent of route_stop_id -- a component can carry either, both, or neither.
    is_accommodation INTEGER NOT NULL DEFAULT 0,  -- Hotel/Room vs Other Service -- the two Costing sub-parts a Day's card shows separately (see view.html's day_card macro); same independent-flag pattern as is_layover/is_checkpoint on package_route_stops
    is_airline_ticket INTEGER NOT NULL DEFAULT 0,  -- per Zeb's request: flags a whole-trip (no route_stop_id, no day_id) component as an Airline Ticket, shown in its own "Airline Tickets" section on the package page -- between Route and Entire Tour Services -- instead of folded into the generic Entire Tour Services list. Same independent-flag pattern as is_accommodation; only meaningful for a component with no day_id (a Day-tied item lives on its Day's Accommodation/Other Service table regardless of this flag).
    repeat_for_checkpoint INTEGER NOT NULL DEFAULT 0,  -- per Zeb's request: checked once on the Day this line is added to, it then also shows (same row, same component_id -- edit/delete from either place affects it everywhere) on every OTHER Day within that Checkpoint, so a multi-night item like a hotel room only has to be entered once. Only meaningful for a component whose day_id belongs to a Checkpoint-flagged stop; harmless no-op otherwise. See view_package()'s repeat-inheritance pass and day_card's "is_inherited" rendering.
    component_type  TEXT NOT NULL CHECK (component_type IN ('service','product','custom')),
    service_id      INTEGER REFERENCES services(service_id),
    product_id      INTEGER REFERENCES products(product_id),
    supplier_id     INTEGER REFERENCES suppliers(supplier_id),
    description     TEXT,                   -- required for 'custom'; optional display override otherwise
    quantity        NUMERIC NOT NULL DEFAULT 1,  -- multiplies against unit_cost/unit_price for the Total Cost/Total Price/Profit Margin columns shown on the Day card's Hotel/Room and Other Service tables (see view.html) -- e.g. 3 rooms x $150/night
    unit            TEXT,                   -- e.g. 'per room/night', 'per leg', 'per day', 'per pax'
    unit_cost       NUMERIC,
    unit_price      NUMERIC,
    currency        TEXT,
    notes           TEXT,
    sequence_number INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_package_components_package ON package_components(package_id);
CREATE INDEX idx_package_components_stop ON package_components(route_stop_id);
CREATE INDEX idx_package_components_day ON package_components(day_id);
CREATE INDEX idx_package_components_service ON package_components(service_id);
CREATE INDEX idx_package_components_product ON package_components(product_id);
CREATE INDEX idx_package_components_supplier ON package_components(supplier_id);
CREATE INDEX idx_package_components_tenant ON package_components(tenant_id);

-- Per-person price by group-size band -- coaches/guides are often
-- fixed-cost per group, so price-per-pax drops as the group grows.
CREATE TABLE package_price_tiers (
    tier_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    package_id      INTEGER NOT NULL REFERENCES packages(package_id),
    min_pax         INTEGER NOT NULL,
    max_pax         INTEGER,
    price_per_pax   NUMERIC NOT NULL,
    currency        TEXT,
    notes           TEXT
);
CREATE INDEX idx_package_price_tiers_package ON package_price_tiers(package_id);
CREATE INDEX idx_package_price_tiers_tenant ON package_price_tiers(tenant_id);

-- ============================================================================
-- MODULE U -- Knowledge Graph (AI Agent / Data Enrichment, Phase 1 pilot)
-- ============================================================================
-- Structured replacement for the freeform ';'-delimited knowledge_graph_data
-- text column already sitting on contacts/points_of_interest/suppliers/
-- organizations (each explicitly commented "Phase 2: structured triples").
-- One row = one directed edge between two entities, e.g.
--   (PointOfInterest, <Gurdwara Janam Asthan>) --[near]--> (Supplier, <Hotel One Nankana Sahib>)
-- Entity type list is intentionally the small set of tables this pilot
-- actually links; extend the CHECK constraint (both columns) when a new
-- entity type needs to participate. The existing freeform text fields are
-- left in place (still shown in the UI) rather than removed -- this table
-- is additive, not a replacement migration.
CREATE TABLE knowledge_graph_edges (
    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('Supplier','PointOfInterest','City','Organization','Contact')),
    subject_id      INTEGER NOT NULL,
    relationship    TEXT NOT NULL,           -- freeform label, e.g. 'located_in', 'near', 'served_by', 'operates_tours_to', 'nearest_airport'
    object_type     TEXT NOT NULL CHECK (object_type IN ('Supplier','PointOfInterest','City','Organization','Contact')),
    object_id       INTEGER NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, subject_type, subject_id, relationship, object_type, object_id)
);
CREATE INDEX idx_kg_edges_tenant ON knowledge_graph_edges(tenant_id);
CREATE INDEX idx_kg_edges_subject ON knowledge_graph_edges(tenant_id, subject_type, subject_id);
CREATE INDEX idx_kg_edges_object ON knowledge_graph_edges(tenant_id, object_type, object_id);

-- ============================================================================
-- MODULE W -- Supplier Type Templates: Hotel (Sept 2026)
-- ============================================================================
-- Per Zeb's request: "For Each Supplier Type, there will be a Template
-- linked to the Supplier" -- a Type-specific set of extra attributes shown
-- on the Supplier form/view (see supplier_types.template_key above). This
-- phase builds the one Template actually specified: Hotel, with (1) an
-- Amenities & Facilities checklist and (2) a Room Types / room-count
-- table. A future Template (e.g. Transport) gets its own template_key and
-- its own tables, following this same shape.

-- The master "Template" list of possible Hotel amenities/facilities,
-- grouped into fixed sub-sections, each with a display icon (a Bootstrap
-- Icons class name, e.g. 'bi-wifi' -- the app already loads Bootstrap
-- Icons). This table IS where "the display icons can be kept" per the
-- request -- edit a row's `icon` to change what's shown, no code change
-- needed. Tenant-scoped like every other lookup table; seeded from
-- Amenities_and_Facilities1a.txt's exact category/item list (see
-- seed_data.HOTEL_AMENITY_OPTIONS) -- the specific icon choices are a
-- first pass, easily swapped later.
-- Nested under supplier_types (parent = the 'Hotel' row), same pattern as
-- supplier_subtypes -- lets Table Maintenance manage this list directly
-- (Zeb, Sept 2026: "Amenities Options and Room Type will be Lookup tables
-- associated with Supplier Type Hotel"). `description` is the standard
-- lookup-table column (unused by the Amenities & Facilities form itself,
-- but present for consistency with every other Table Maintenance table).
CREATE TABLE hotel_amenity_options (
    amenity_option_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id),  -- parent -- the Supplier Type (Template) this option belongs to, e.g. 'Hotel'
    category        TEXT NOT NULL,           -- 'In-Room' | 'Food & Drink' | 'Wellness' | 'Business' | 'Convenience' -- the checklist form's sub-sections
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    icon            TEXT,                    -- Bootstrap Icons class name, e.g. 'bi-wifi' (without the leading 'bi ' base class -- the template adds that)
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_hotel_amenity_options_tenant ON hotel_amenity_options(tenant_id);
CREATE INDEX idx_hotel_amenity_options_supplier_type ON hotel_amenity_options(supplier_type_id);

-- Which of those amenities a given Hotel Supplier actually offers --
-- presence of a row = checked, same convention as supplier_amenities'
-- sibling junction tables elsewhere in this schema. One row per
-- (supplier, amenity).
CREATE TABLE supplier_amenities (
    supplier_amenity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    amenity_option_id INTEGER NOT NULL REFERENCES hotel_amenity_options(amenity_option_id),
    UNIQUE (tenant_id, supplier_id, amenity_option_id)
);
CREATE INDEX idx_supplier_amenities_supplier ON supplier_amenities(supplier_id);
CREATE INDEX idx_supplier_amenities_option ON supplier_amenities(amenity_option_id);

-- Master list of standard Room Types (Single/Double/Twin/Queen/King/
-- Double-Double/Queen-Queen), each carrying the request's own description
-- as a sensible per-tenant default (overridable per-supplier below).
-- Tenant-scoped lookup, seeded from seed_data.HOTEL_ROOM_TYPES.
-- Nested under supplier_types (parent = the 'Hotel' row), same reasoning
-- as hotel_amenity_options above. `description` here IS the field a
-- Supplier's own Room row (supplier_rooms.description) defaults from and
-- can override -- named "description", not "default_description", so it
-- lines up with the standard Table Maintenance column set.
CREATE TABLE hotel_room_types (
    room_type_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_type_id INTEGER REFERENCES supplier_types(supplier_type_id),  -- parent -- the Supplier Type (Template) this Room Type belongs to, e.g. 'Hotel'
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_hotel_room_types_tenant ON hotel_room_types(tenant_id);
CREATE INDEX idx_hotel_room_types_supplier_type ON hotel_room_types(supplier_type_id);

-- One row per (Hotel Supplier, Room Type) -- "the Room Types and Number of
-- Rooms for each Type" table shown on the Hotel form. Total Number of
-- Rooms in the hotel is computed as SUM(number_of_rooms) at display time,
-- not stored, so it can never drift out of sync.
CREATE TABLE supplier_rooms (
    supplier_room_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    room_type_id    INTEGER NOT NULL REFERENCES hotel_room_types(room_type_id),
    description     TEXT,                    -- optional override of the Room Type's own description
    number_of_rooms INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, supplier_id, room_type_id)
);
CREATE INDEX idx_supplier_rooms_supplier ON supplier_rooms(supplier_id);
CREATE INDEX idx_supplier_rooms_type ON supplier_rooms(room_type_id);

-- ============================================================================
-- MODULE X -- Supplier Documents, Links and Images (Sept 2026)
--
-- Per Zeb's request: "In the Suppliers Form and Table, please add a
-- sub-module called 'Documents, Links and Images'. Each supplier's copies
-- of business licenses, permissions, rules and regulations, agreements and
-- Images / photographs will be stored in this sub-module. The example form
-- from 'Organization Intelligence/Documents' can be used as reference."
--
-- Mirrors MODULE D's content/content_locations/content_keywords/
-- content_hashtags shape almost exactly (same Details + multi-location
-- pattern: a document can carry both a Local Drive Path copy and a Cloud
-- Link, or several of either), scoped to one Supplier instead of being a
-- tenant-wide, unattached knowledge base. Two deliberate differences from
-- the Module D reference form:
--   1. No Knowledge Domains link -- "Notice I have removed 'Knowledge
--      domains' as that is not required for Suppliers."
--   2. No Contacts Link sub-feature -- out of scope for what was asked
--      (business licenses/permits/rules/agreements/images), and Suppliers
--      already has its own Contacts sub-module for people.
-- Everything else (Author(s)/Description/Notes/Keywords/Hashtags on the
-- document, Local Drive Path + Cloud Link locations, browse-for-file) is
-- carried over so the UI and workflow feel identical to the reference.
-- ============================================================================

-- Lookup for supplier_documents.document_type_id below -- what KIND of
-- document this is (Business License, Permit, Agreement, ...). A flat
-- lookup, not nested under supplier_types like hotel_amenity_options/
-- hotel_room_types -- every Supplier Type can have licenses/permits/
-- agreements/images, so this isn't Hotel-specific. Table Maintenance-
-- managed, seeded from seed_data.SUPPLIER_DOCUMENT_TYPES.
CREATE TABLE supplier_document_types (
    document_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    code            TEXT,
    label           TEXT NOT NULL,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, code)
);
CREATE INDEX idx_supplier_document_types_tenant ON supplier_document_types(tenant_id);

-- One row per document/link/image record on a Supplier's "Documents, Links
-- and Images" card. Mirrors `content` (MODULE D) minus knowledge-domain
-- linkage; is_deleted follows the same soft-delete convention as `content`.
CREATE TABLE supplier_documents (
    supplier_document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    document_name   TEXT NOT NULL,
    document_type_id INTEGER REFERENCES supplier_document_types(document_type_id),
    authors         TEXT,                    -- e.g. issuing authority/signatory -- optional, same field as Module D's Author(s)
    description     TEXT,
    notes           TEXT,                    -- brief summary; web URLs are auto-linked on display
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_supplier_documents_tenant ON supplier_documents(tenant_id);
CREATE INDEX idx_supplier_documents_supplier ON supplier_documents(supplier_id);
CREATE INDEX idx_supplier_documents_type ON supplier_documents(document_type_id);

-- Where a copy of the document actually lives -- a local drive path and/or
-- a cloud link, any number of each ("copies... will be stored" -- a
-- license might have both a scanned local file and a cloud-drive backup
-- link). Mirrors content_locations exactly.
CREATE TABLE supplier_document_locations (
    location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
    location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path')),
    path_or_url     TEXT NOT NULL
);
CREATE INDEX idx_supplier_document_locations_tenant ON supplier_document_locations(tenant_id);
CREATE INDEX idx_supplier_document_locations_document ON supplier_document_locations(supplier_document_id);

CREATE TABLE supplier_document_keywords (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
    term            TEXT NOT NULL,
    PRIMARY KEY (supplier_document_id, term)
);
CREATE INDEX idx_supplier_document_keywords_tenant ON supplier_document_keywords(tenant_id);

CREATE TABLE supplier_document_hashtags (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    supplier_document_id INTEGER NOT NULL REFERENCES supplier_documents(supplier_document_id),
    term            TEXT NOT NULL,
    PRIMARY KEY (supplier_document_id, term)
);
CREATE INDEX idx_supplier_document_hashtags_tenant ON supplier_document_hashtags(tenant_id);

-- ============================================================================
-- MODULE Y -- Point of Interest Images/Photographs & Structured Links (Sept 2026)
--
-- Per Zeb's request: "Add (1) 'Images/Photographs' to 'Point Of Interest'
-- form. (2) Bulk import Images. (3) Fix the Links section for Link and
-- Description as shown in the attached image."
--
-- poi_images -- one row per image/photograph, each with its own Cloud Link
-- or Local Drive Path (mirrors supplier_document_locations' shape exactly),
-- reached via a "Bulk Import Images" flow identical in spirit to the one
-- just built for Suppliers (utils.pick_files_dialog). No "document" wrapper
-- table is needed here the way Suppliers has supplier_documents -- Zeb only
-- asked for Images/Photographs on POIs, not a general license/permit system
-- -- so each image IS its own row.
CREATE TABLE poi_images (
    poi_image_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
    location_type   TEXT CHECK (location_type IN ('Cloud Link','Local Drive Path')),
    path_or_url     TEXT NOT NULL,
    caption         TEXT,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_poi_images_tenant ON poi_images(tenant_id);
CREATE INDEX idx_poi_images_poi ON poi_images(poi_id);

-- poi_reference_links -- replaces the single freeform points_of_interest.
-- links textarea with the structured, multi-row "Link #1/#2/#3 + Description"
-- editor from Zeb's mockup. Mirrors the shape (and the role) already
-- established elsewhere in the app for organization_reference_links /
-- supplier_reference_links / contact_reference_links -- just url +
-- description here, since that's all the mockup showed (no document_path/
-- notes columns, to keep the inline row editor as simple as the mockup).
-- The legacy points_of_interest.links column is left in place, untouched,
-- for audit -- migrate_add_poi_images_links.py copies any non-blank legacy
-- text into one poi_reference_links row per line so nothing is lost, and
-- the form itself no longer writes to points_of_interest.links going
-- forward.
CREATE TABLE poi_reference_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(tenant_id),
    poi_id          INTEGER NOT NULL REFERENCES points_of_interest(poi_id),
    url             TEXT,
    description     TEXT,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_poi_reference_links_tenant ON poi_reference_links(tenant_id);
CREATE INDEX idx_poi_reference_links_poi ON poi_reference_links(poi_id);
