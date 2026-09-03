"""
Shared free-text postal address parsing.

Splits one free-text "Street, ..., City, Province, Country" column into the
fields a structured address table actually has (street / region_id /
country_id / state_id+state_province_text / city_id+city_text /
postal_code), matching City/Province/Country against the GLOBAL geography
lookups (regions/countries/states/cities — the same tables the Region ->
Country -> Province/State -> City cascade on every address form uses) so a
parsed address behaves identically to one entered by hand. A part that
isn't recognized falls back to the matching free-text column rather than
being dropped, exactly like the manual address forms' "not on file"
fallback.

Originally built for the Suppliers CSV importer (blueprints/
data_exchange.py, against Pakistani business-directory addresses) and
generalized here so the Organizations address migration/importer can reuse
the same logic against a very different address style (US Sikh-diaspora
organization addresses, e.g. "P.O. Box 3635, Columbus, Ohio 43210 United
States of America") rather than re-implementing it — see
parse_free_text_address() below for the parsing heuristic itself.
"""
import re

# Common short/alternate country-name spellings this parser should treat as
# the country's actual seeded label (see seed_data.COUNTRIES — the full
# ISO 3166-1 name, e.g. "United States", not "United States of America" or
# "USA"). Extend this as new source files turn up new spellings; an exact
# match against the seeded label is always tried first, so this is only a
# fallback for the handful of everyday abbreviations real address text
# actually uses.
COUNTRY_NAME_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.a": "United States",
    "u.s.a.": "United States", "u.s": "United States", "u.s.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "u.k": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom",
}

# USPS two-letter codes for US states (+ DC), and the standard two-letter
# codes for Canadian provinces/territories — the states table stores full
# names ("New York", "Ontario"), but real address data very often gives the
# abbreviation instead (e.g. "..., New York, NY 10006, USA", or "...,
# Ottawa, ON K1H 7Z6, Canada"). Tried as a fallback in match_state, after an
# exact match against the full seeded label.
STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}


def split_address_parts(address):
    return [p.strip() for p in (address or "").split(",") if p.strip()]


# Pakistani business directories commonly name the *district* rather than
# the city/town at the position right before the province (e.g. "...,
# Patoki, Distt. Kasur, Punjab, Pakistan") — stripping this prefix before
# matching against the cities lookup turns "Distt. Kasur" into "Kasur",
# which the seeded Punjab cities list actually has, instead of failing to
# match and falling back to a literal "Distt. Kasur" free-text city.
DISTRICT_PREFIXES = ("distt.", "distt", "district", "tehsil")


def strip_district_prefix(text):
    t = (text or "").strip()
    low = t.lower()
    for prefix in DISTRICT_PREFIXES:
        if low.startswith(prefix):
            rest = t[len(prefix):].strip(" .")
            if rest:
                return rest
    return t


def match_country(db, name):
    name = (name or "").strip()
    if not name:
        return None
    row = db.execute("SELECT country_id, region_id FROM countries WHERE lower(label) = lower(?)", (name,)).fetchone()
    if row:
        return row
    alias_target = COUNTRY_NAME_ALIASES.get(name.lower())
    if alias_target:
        return db.execute(
            "SELECT country_id, region_id FROM countries WHERE lower(label) = lower(?)", (alias_target,)
        ).fetchone()
    return None


def _all_country_names(db):
    """Every recognized country name/alias, longest first, so a suffix scan
    (see match_country_suffix) prefers the most specific match — e.g.
    "united states of america" (an alias) over the shorter "united states"
    (the seeded label) when both would otherwise match the same tail."""
    labels = [r["label"] for r in db.execute("SELECT label FROM countries").fetchall()]
    names = set(labels) | set(COUNTRY_NAME_ALIASES.keys()) | set(COUNTRY_NAME_ALIASES.values())
    return sorted(names, key=len, reverse=True)


def match_country_suffix(db, text):
    """Finds a known country name/alias as a trailing, word-bounded suffix
    of `text` even when nothing separates it from whatever comes right
    before it — e.g. "Ohio 43210 United States of America" has no comma
    before the country name, unlike a cleanly comma-separated address, so
    the usual "last comma-separated part is the Country" split (see
    parse_free_text_address) leaves it stuck onto the end of the state/zip
    instead of in a part by itself. Returns (country_row, leftover_text) —
    leftover is what's left after stripping the matched suffix, still
    needing its own state/postal-code handling — or None if no known
    country name/alias matches anywhere at the end of `text` (a whole-string
    exact match is match_country's job, not this one)."""
    text = (text or "").strip()
    if not text:
        return None
    low = text.lower()
    for name in _all_country_names(db):
        name_low = name.lower()
        if low == name_low:
            continue  # whole-string match — match_country already covers this case
        if not low.endswith(name_low):
            continue
        prefix_len = len(text) - len(name_low)
        if prefix_len == 0:
            continue
        boundary_char = text[prefix_len - 1]
        if not (boundary_char.isspace() or boundary_char in ",."):
            continue  # e.g. "Chinatown" ending in "...china" — not a real word boundary
        row = match_country(db, name)
        if row:
            return row, text[:prefix_len].strip(" ,.")
    return None


def country_has_seeded_states(db, country_id):
    """Whether the states lookup has ANY rows at all for this country —
    currently true only for Pakistan (see module docstring in seed_data.py:
    the states/cities tables are Pakistan-only so far). Used by
    parse_free_text_address to decide, when a candidate province/state
    segment doesn't match anything, whether that's because it genuinely
    isn't a province (Pakistani addresses often skip the province and go
    straight to City) or because this country's provinces simply aren't in
    the lookup yet (true of every non-Pakistan country right now, e.g. a US
    "City, State ZIP, Country" address) — those two situations need
    different handling, see the parsing heuristic's docstring below."""
    if not country_id:
        return False
    return db.execute("SELECT 1 FROM states WHERE country_id = ? LIMIT 1", (country_id,)).fetchone() is not None


def match_state(db, country_id, name):
    if not country_id or not name:
        return None
    row = db.execute(
        "SELECT state_id FROM states WHERE country_id = ? AND lower(label) = lower(?)", (country_id, name)
    ).fetchone()
    if row:
        return row
    full_name = STATE_ABBREVIATIONS.get(name.strip().upper())
    if full_name:
        return db.execute(
            "SELECT state_id FROM states WHERE country_id = ? AND lower(label) = lower(?)", (country_id, full_name)
        ).fetchone()
    return None


def match_city(db, state_id, name):
    if not state_id or not name:
        return None
    return db.execute(
        "SELECT city_id FROM cities WHERE state_id = ? AND lower(label) = lower(?)", (state_id, name)
    ).fetchone()


def match_city_anywhere(db, country_id, name):
    """Matches a city by name across every province of one country, rather
    than requiring a specific state_id — used when an address doesn't
    spell out a separate province at all (see parse_free_text_address's
    "no explicit province" branch), since the matched city's own province
    can then be used to fill that gap in automatically."""
    if not country_id or not name:
        return None
    return db.execute(
        """SELECT ci.city_id, ci.state_id FROM cities ci
           JOIN states st ON st.state_id = ci.state_id
           WHERE st.country_id = ? AND lower(ci.label) = lower(?)""",
        (country_id, name),
    ).fetchone()


def clean_address_text(text):
    """Strips parenthetical asides and anything after a stray semicolon
    before structural parsing — e.g. "..., Lahore 54000, Pakistan (Head
    Office); also Multan operational base" -> "..., Lahore 54000,
    Pakistan". This kind of annotation is real information (callers keep
    the original text elsewhere for the record — e.g. in Notes, or the raw
    imported row), but it isn't part of the postal address structure and
    otherwise throws off the City/Province/Country split below — a
    trailing "(Head Office)" or "(offices also referenced in Peshawar,
    Karimabad, Karachi)" would otherwise get split on its own internal
    commas right along with the real address."""
    text = (text or "").split(";")[0]
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,+", ",", text)
    return text.strip(" ,")


# US ZIP (5 or 5+4, the +4 part handled separately since it's usually
# hyphenated onto the 5-digit code rather than space-separated) and
# Pakistani postal codes are both plain digit runs in this range; Canadian
# postal codes are letter-digit-letter [space] digit-letter-digit instead
# (e.g. "K1H 7Z6").
_POSTAL_SUFFIX_RE = re.compile(
    r"^(.*\S)\s+(\d{4,6}(?:-\d{4})?|[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d)$"
)


def split_trailing_postal_code(text):
    """"Karachi 75530" -> ("Karachi", "75530"); "Columbus" -> ("Columbus",
    ""); "ON K1H 7Z6" -> ("ON", "K1H 7Z6") — some source addresses run the
    postal code straight into the city/state/province name with just a
    space, rather than giving it its own comma-separated part."""
    m = _POSTAL_SUFFIX_RE.match(text or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (text or "").strip(), ""


# Matches "(419) 535-6794", "419-535-6794", "419.535.6794", "4195356794" —
# phone numbers that ended up embedded in an address column's free text
# (see extract_trailing_phones) rather than given their own field.
_EMBEDDED_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


def extract_trailing_phones(text):
    """Pulls every phone-number-looking substring out of a free-text
    address column and returns (remaining_text, [phone, ...]) — some
    imported address data has one or more phone numbers run on to the end
    with no proper field of their own (e.g. "..., Columbus, Ohio 43210
    United States of America,  (419) 535-6794 (614) 210-0591"). Numbers
    are matched anywhere in the text, not just the tail, since real data
    doesn't always put them cleanly at the end."""
    text = text or ""
    phones = [m.strip() for m in _EMBEDDED_PHONE_RE.findall(text)]
    cleaned = _EMBEDDED_PHONE_RE.sub("", text)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*,+", ",", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip(" ,"), phones


def parse_free_text_address(db, address_text):
    """Splits one free-text address column into the fields a structured
    address table actually has (see module docstring for the exact
    field-name contract). Caller passes the already phone-stripped text if
    it also needs extract_trailing_phones() — this function does not call
    that itself, since not every source needs it (the Suppliers CSV,
    for instance, keeps phone numbers in their own column already).

    Heuristic, after clean_address_text strips asides (see above): the
    LAST comma-separated part is Country (matched against the seeded
    country list, with common alternate spellings like "USA" recognized —
    see COUNTRY_NAME_ALIASES). When that whole last part doesn't match
    (e.g. "Ohio 43210 United States of America" — no comma at all before
    the country name, so it's stuck onto the end of the state/zip instead
    of sitting in its own part), a known country name/alias is instead
    searched for as a trailing word-bounded chunk of it (see
    match_country_suffix); whatever's left after peeling that off becomes
    the next part to examine, in place of popping one normally.

    That next part is checked against the country's own seeded provinces
    first — when it matches (e.g. "..., Karachi, Sindh, Pakistan"), the
    part before THAT is the City. Many addresses in practice skip the
    province entirely (e.g. "..., Islamabad, Pakistan", or "..., Lahore
    54000, Pakistan" with the postal code run into the city name) — when
    the pre-country part does NOT match a known province AND this country
    HAS provinces seeded at all (see country_has_seeded_states — currently
    only Pakistan does), it's tried instead as a City (after stripping a
    trailing postal code, and a "Distt./District/Tehsil " prefix some
    Pakistani directories put in the city's place — see
    strip_district_prefix), searched across every province of the matched
    country; a match there fills in the missing province automatically
    from that city's own record.

    For every other country (none of their provinces are seeded yet, so a
    failed match there is uninformative either way), a different
    assumption applies instead, since these addresses are overwhelmingly
    "Street, City, State ZIP, Country" (e.g. "165 Broadway, New York, NY
    10006, USA" or the Ohio example above): the part is treated as the
    State (free-text, postal code split off), and — provided there's a
    further part still available — the City is read from one part further
    back (also free-text, though still checked against the seeded cities
    in case it happens to be one). This only fires when a further part
    exists to serve as the City; a single leftover part with nowhere else
    to pull a City from still falls back to being read as the City itself,
    same as the seeded-province countries above.

    Whatever's left over after all of this is rejoined as Street.

    A part that isn't recognized falls back to the matching free-text
    column (state_province_text / city_text) rather than being dropped,
    exactly like the manual address forms' "not on file" fallback — so an
    unmatched result is never silent data loss, just an address that isn't
    linked to a geography lookup row yet."""
    parts = split_address_parts(clean_address_text(address_text))
    result = {
        "street": "", "region_id": None,
        "country_id": None, "country_text": "",
        "state_id": None, "state_text": "",
        "city_id": None, "city_text": "",
        "postal_code": "",
    }
    if not parts:
        return result
    remaining = list(parts)

    country_name = remaining.pop() if remaining else ""
    country_row = match_country(db, country_name)
    result["country_text"] = country_name
    leftover_from_country = None
    if country_row:
        result["country_id"] = country_row["country_id"]
        result["region_id"] = country_row["region_id"]
    else:
        suffix_match = match_country_suffix(db, country_name)
        if suffix_match:
            country_row, leftover_from_country = suffix_match
            result["country_id"] = country_row["country_id"]
            result["region_id"] = country_row["region_id"]

    # Normally the next part in is popped fresh off `remaining`; when the
    # country was instead peeled off the tail of a part with no comma of
    # its own (leftover_from_country set, just above), what's left of that
    # SAME part takes its place instead, without an extra pop.
    candidate = leftover_from_country if leftover_from_country is not None else (remaining.pop() if remaining else "")
    # A trailing postal code sometimes runs into this part too (e.g. "Ohio
    # 43210") even when it turns out to be the Province, not the City —
    # strip it up front so the province match isn't defeated by it, and
    # keep whatever's found as a fallback postal_code either way.
    candidate_clean, candidate_postal = split_trailing_postal_code(candidate) if candidate else (candidate, "")
    state_row = match_state(db, result["country_id"], candidate_clean) if candidate_clean else None
    if state_row:
        # Explicit, seeded province matched — the next part in is the City.
        result["state_id"] = state_row["state_id"]
        result["state_text"] = candidate_clean
        result["postal_code"] = candidate_postal
        city_name = remaining.pop() if remaining else ""
        result["city_text"] = city_name
        city_row = match_city(db, result["state_id"], strip_district_prefix(city_name)) if city_name else None
        if city_row:
            result["city_id"] = city_row["city_id"]
    elif candidate and result["country_id"] and not country_has_seeded_states(db, result["country_id"]) and remaining:
        # This country has no seeded provinces to match against at all
        # (true of every country except Pakistan right now) — assume the
        # common "City, State ZIP, Country" shape: this part is the State
        # (free-text; a trailing ZIP/postal code split off), and the real
        # City is one part further back, still available in `remaining`.
        result["state_text"] = candidate_clean
        result["postal_code"] = candidate_postal
        city_name = remaining.pop()
        result["city_text"] = city_name
        city_row = match_city_anywhere(db, result["country_id"], strip_district_prefix(city_name))
        if city_row:
            result["city_id"] = city_row["city_id"]
            # This country has no seeded states (that's how we got here), so
            # there's no state_id to also fill in from the city match, unlike
            # the equivalent match_city_anywhere call below — state_text above
            # already carries whatever free-text state this address gave.
    elif candidate:
        # Either this country's provinces ARE seeded and simply didn't match
        # this part (Pakistani addresses often skip the province entirely,
        # going straight from City to Country), or there was nowhere further
        # back to pull a separate City from — either way, `candidate` is
        # most likely the City itself (possibly with a trailing postal
        # code). Matching it against the country's cities across every
        # province recovers a missing province automatically from the match.
        city_candidate, postal = split_trailing_postal_code(strip_district_prefix(candidate))
        result["postal_code"] = postal
        result["city_text"] = candidate
        city_row = match_city_anywhere(db, result["country_id"], city_candidate)
        if city_row:
            result["city_id"] = city_row["city_id"]
            result["state_id"] = city_row["state_id"]
            state_label = db.execute("SELECT label FROM states WHERE state_id = ?", (city_row["state_id"],)).fetchone()
            result["state_text"] = state_label["label"] if state_label else ""

    result["street"] = ", ".join(remaining)
    return result
