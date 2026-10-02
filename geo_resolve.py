"""
Resolves place names from an import file (country, province/state, city)
to TMS geography, tolerating spelling differences -- "Gilgit-Baltistan"
finds TMS's "Gilgit Baltisitan", "Balochistan" finds "Baluchistan".

One Resolver per import run: it loads the geography once and can create
missing provinces and cities when the import allows it (they're added to
its cache straight away, so later rows in the same file find them).
"""
from fuzzy import compare, normalize, similarity, split_alt_names

COUNTRY_ALIASES = {"uk": "GB", "united kingdom": "GB", "great britain": "GB", "usa": "US", "united states": "US",
                   "us": "US", "uae": "AE", "prc": "CN", "peoples republic of china": "CN",
                   "south korea": "KR", "korea south": "KR", "russia": "RU", "iran": "IR", "turkey": "TR",
                   "turkiye": "TR", "vietnam": "VN", "laos": "LA", "syria": "SY", "tanzania": "TZ",
                   "czech republic": "CZ", "czechia": "CZ", "netherlands": "NL", "holland": "NL",
                   "hong kong": "HK", "hong kong sar": "HK", "china hong kong sar": "HK", "macau": "MO",
                   "macao": "MO", "taiwan": "TW", "palestine": "PS", "ivory coast": "CI", "cote divoire": "CI"}

# Place names are short, so "sounds alike" alone is too loose for them
# (Daud is not Dadu, Shahwali is not Sahiwal): a sounds-alike city or
# province is only accepted when the spellings are this close as well.
PLACE_SOUNDS_MIN = 0.85

_RANK = {"same": 3, "sounds": 2, "similar": 1}


class Resolver:
    def __init__(self, db):
        self.db = db
        self.countries = [dict(r) for r in db.execute("SELECT country_id, code, label, region_id FROM countries")]
        self.states = [dict(r) for r in db.execute("SELECT state_id, country_id, code, label FROM states")]
        self.cities = [dict(r) for r in db.execute(
            "SELECT ci.city_id, ci.label, ci.state_id, s.country_id, ci.alt_names FROM cities ci "
            "JOIN states s ON s.state_id = ci.state_id")]

    # ---- countries
    def country(self, text):
        """(country_row or None, how)."""
        if not text:
            return None, None
        t = text.strip()
        code = COUNTRY_ALIASES.get(normalize(t, drop_filler=False), t.upper() if len(t) in (2, 3) else None)
        if code:
            for c in self.countries:
                if (c["code"] or "").upper() == code:
                    return c, "same"
        row, how = self._best(t, self.countries, lambda c: [c["label"]])
        if row is None and "(" in t:  # "China (Hong Kong SAR)" -> try "Hong Kong SAR"
            inner = t[t.index("(") + 1:t.rindex(")")] if ")" in t else t[t.index("(") + 1:]
            return self.country(inner)
        return row, how

    # ---- provinces / states
    def state(self, country_id, text):
        if not text or not country_id:
            return None, None
        pool = [s for s in self.states if s["country_id"] == country_id]
        t = text.strip()
        for s in pool:
            if s["code"] and s["code"].upper() == t.upper():
                return s, "same"
        return self._strict(t, *self._best(t, pool, lambda s: [s["label"]]), lambda s: [s["label"]])

    def add_state(self, country_id, label):
        cur = self.db.execute(
            "INSERT INTO states (country_id, label, sort_order, is_active) VALUES (?, ?, 0, 1)", (country_id, label))
        row = {"state_id": cur.lastrowid, "country_id": country_id, "code": None, "label": label}
        self.states.append(row)
        return row

    # ---- cities
    def city(self, country_id, state_id, text):
        """Best city match in the country, preferring the given province.
        Returns (city_row or None, how)."""
        if not text or not country_id:
            return None, None
        pool = [c for c in self.cities if c["country_id"] == country_id]
        names = lambda c: [c["label"]] + split_alt_names(c["alt_names"])  # noqa: E731
        if state_id:
            in_state = [c for c in pool if c["state_id"] == state_id]
            row, how = self._strict(text, *self._best(text, in_state, names), names)
            if row and how in ("same", "sounds"):
                return row, how
        return self._strict(text, *self._best(text, pool, names), names)

    def add_city(self, state_id, label, **extra):
        cols = {"state_id": state_id, "label": label, "sort_order": 0, "is_active": 1}
        cols.update({k: v for k, v in extra.items() if v is not None})
        cur = self.db.execute(f"INSERT INTO cities ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              tuple(cols.values()))
        country_id = next(s["country_id"] for s in self.states if s["state_id"] == state_id)
        row = {"city_id": cur.lastrowid, "label": label, "state_id": state_id, "country_id": country_id,
               "alt_names": None}
        self.cities.append(row)
        return row

    def region_for(self, country_id):
        return next((c["region_id"] for c in self.countries if c["country_id"] == country_id), None)

    # ---- shared
    @staticmethod
    def _strict(text, row, how, names):
        """Downgrade a sounds-alike place match to 'similar' (= not accepted,
        offered as a hint) unless the spellings are also close."""
        if how == "sounds" and max(similarity(text, n) for n in names(row)) < PLACE_SOUNDS_MIN:
            return row, "similar"
        return row, how

    @staticmethod
    def _best(text, pool, names):
        """Strongest match in pool: 'same' beats 'sounds' beats 'similar'."""
        best, best_how = None, None
        for item in pool:
            names_ = names(item)
            how = compare(text, names_[0], names_[1:])
            if how and (best_how is None or _RANK[how] > _RANK[best_how]):
                best, best_how = item, how
                if how == "same":
                    break
        return best, best_how
