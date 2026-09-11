"""
Small shared helpers used across blueprints/templates.
"""
import os
import re
import subprocess
import sys
from datetime import datetime

from markupsafe import Markup, escape

_URL_RE = re.compile(r'((?:https?://|www\.)[^\s<>"\']+)', re.IGNORECASE)


def _clean(value):
    """Normalize a possibly-missing phone field to a real absent value.

    Treats None, blank/whitespace-only strings, and the literal text "none"
    (any case — a defensive guard in case bad data ever got typed or
    imported as the word "None" rather than left blank) all the same way:
    as absent. Returns a stripped string, or None if absent.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    return text


def _format_country_code(country):
    """Normalize a phone country code for display: always a leading '+',
    never a leading '0' — so '01' (a common way people type it, echoing
    the international trunk prefix) renders as '+1', and '+1' typed
    correctly is left untouched (idempotent)."""
    if not country:
        return country
    if country.startswith("+"):
        return country
    digits = country.lstrip("0")
    return f"+{digits}" if digits else country


def format_phone(p):
    """Render a contact_phones/platform_phones/account phone row as a clean
    display string — no stray "None"/"xNone" when country code, area code,
    or extension are absent (many countries, e.g. Pakistan, don't use area
    codes for mobile numbers, so this is the common case, not the
    exception). Usable as a Jinja filter: {{ p | format_phone }}.
    """
    d = dict(p) if p is not None else {}
    country = _format_country_code(_clean(d.get("country_code")))
    area = _clean(d.get("area_code"))
    number = _clean(d.get("number")) or ""
    ext = _clean(d.get("extension"))

    parts = [part for part in (country, area, number) if part]
    result = " ".join(parts)
    if ext:
        result += f" x{ext}"
    return result


def linkify(text):
    """Render freeform text as safe HTML with any http(s)/www URLs turned
    into clickable links and newlines preserved. The rest of the text is
    HTML-escaped first, so this is safe to mark as |safe in a template.
    Usable as a Jinja filter: {{ item.notes | linkify }}.
    """
    if not text:
        return ""
    escaped = str(escape(text))

    def _replace(m):
        raw = m.group(1)
        trailing = ""
        while raw and raw[-1] in ".,;:!?)]}'\"":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        href = raw if raw.lower().startswith(("http://", "https://")) else f"https://{raw}"
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{raw}</a>{trailing}'

    linked = _URL_RE.sub(_replace, escaped)
    return Markup(linked.replace("\n", "<br>"))


# Recognizes one DMS ("degrees minutes seconds") coordinate component
# anywhere in a string, e.g. "31°35′17″N" or the more commonly pasted
# straight-quote spelling "31°35'17"N" — degree sign required, minutes/
# seconds optional (a bare "31°N" or "31°35′N" both still parse), letter
# case-insensitive. Used by parse_coordinates below to find a latitude
# (N/S) and a longitude (E/W) component regardless of their order or
# separator in the source text, so "74°18′34″E 31°35′17″N" parses exactly
# like "31°35′17″N 74°18′34″E".
_DMS_COMPONENT_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*°\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[′']\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[″\"]?\s*)?)?"
    r"\s*([NSEWnsew])",
)

# Fallback for a coordinate pair with no ° at all — plain decimal degrees,
# e.g. "31.5497, 74.3436", "31.5497N, 74.3436E", or a signed pair with the
# western/southern hemisphere already negative ("31.5497, -74.3436").
# Anchored start-to-end (unlike _DMS_COMPONENT_RE) so a field that merely
# contains two numbers somewhere in unrelated free text doesn't get
# mistaken for coordinates.
_DECIMAL_PAIR_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*([NSns])?\s*[,;]?\s+(-?\d{1,3}(?:\.\d+)?)\s*([EWew])?\s*$"
)


def parse_coordinates(raw):
    """Best-effort parse of a freeform "Map coordinates" field into
    (latitude, longitude) as signed decimal degrees, or None when the text
    isn't recognizable as coordinates at all (a plain description like
    "Near Fort Road roundabout" is left alone, never guessed at).

    Tries DMS components first (_DMS_COMPONENT_RE, via findall so order/
    separator don't matter — the first N/S match found is latitude, the
    first E/W match is longitude), then falls back to a plain decimal pair
    (_DECIMAL_PAIR_RE) when no ° appears anywhere. Returns None instead of
    a value outside the valid range (|lat|>90 or |lon|>180), which usually
    means the two numbers found weren't actually a coordinate pair.
    """
    text = (raw or "").strip()
    if not text:
        return None

    lat = lon = None
    for deg, minute, sec, direction in _DMS_COMPONENT_RE.findall(text):
        value = float(deg) + (float(minute) / 60 if minute else 0) + (float(sec) / 3600 if sec else 0)
        d = direction.upper()
        if d in ("S", "W"):
            value = -value
        if d in ("N", "S") and lat is None:
            lat = value
        elif d in ("E", "W") and lon is None:
            lon = value
    if lat is None or lon is None:
        m = _DECIMAL_PAIR_RE.match(text)
        if not m:
            return None
        lat_v, lat_dir, lon_v, lon_dir = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)
        if lat_dir and lat_dir.upper() == "S":
            lat_v = -abs(lat_v)
        elif lat_dir:
            lat_v = abs(lat_v)
        if lon_dir and lon_dir.upper() == "W":
            lon_v = -abs(lon_v)
        elif lon_dir:
            lon_v = abs(lon_v)
        lat, lon = lat_v, lon_v

    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def format_dms_coordinates(lat, lon):
    """(31.588056, 74.309444) -> '31°35′17″N 74°18′34″E' — the canonical
    display/storage format the Points of Interest "Map coordinates" field
    now standardizes on (real-world DMS notation, degree sign + curly
    prime/double-prime, no space before the N/S/E/W letter)."""

    def part(value, positive, negative):
        direction = positive if value >= 0 else negative
        value = abs(value)
        degrees = int(value)
        minutes_full = (value - degrees) * 60
        minutes = int(minutes_full)
        seconds = round((minutes_full - minutes) * 60)
        if seconds >= 60:
            seconds -= 60
            minutes += 1
        if minutes >= 60:
            minutes -= 60
            degrees += 1
        return f"{degrees}°{minutes:02d}′{seconds:02d}″{direction}"

    return f"{part(lat, 'N', 'S')} {part(lon, 'E', 'W')}"


def normalize_map_coordinates(raw):
    """Converts whatever recognizable coordinate format a user typed or
    pasted (decimal degrees, straight-quote DMS, ...) into the canonical
    DMS display format (see format_dms_coordinates) before it's saved —
    called from blueprints/poi.py's _form_fields so every POI's stored
    map_coordinates ends up in one consistent format regardless of how it
    was entered. Text that doesn't parse as coordinates at all (or is
    blank) is returned unchanged/stripped, never dropped — this is still a
    freeform field for anything that isn't a real coordinate pair."""
    text = (raw or "").strip()
    if not text:
        return text
    parsed = parse_coordinates(text)
    return format_dms_coordinates(*parsed) if parsed else text


def google_maps_url(raw):
    """A googleusercontent-free, plain Google Maps link
    (https://www.google.com/maps?q=lat,lng) for a parseable
    map_coordinates value, or None when it doesn't parse as coordinates —
    callers (map_coordinates_link below) fall back to showing the raw text
    with no link in that case, rather than a broken/nonsensical one."""
    parsed = parse_coordinates(raw)
    if not parsed:
        return None
    lat, lon = parsed
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


def map_coordinates_link(raw):
    """Renders a POI's map_coordinates field as safe HTML: a clickable
    link to Google Maps (opening the exact point in a new tab), labeled
    with the coordinates in the canonical DMS format, when the text
    parses as coordinates — otherwise just the escaped raw text, so a
    non-coordinate freeform value (or one that doesn't parse) still
    displays exactly as entered instead of erroring or vanishing. Usable
    as a Jinja filter: {{ poi.map_coordinates | map_coordinates_link }}.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    display = normalize_map_coordinates(text)
    url = google_maps_url(text)
    if url:
        return Markup(f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(display)}</a>')
    return Markup(escape(display))


def format_date(value):
    """Render a stored 'YYYY-MM-DD' date (the HTML5 date-input format) as
    'Month DD, YYYY' for display — e.g. '1955-05-05' -> 'May 05, 1955'.
    Storage/inputs (forms, CSV import/export) stay ISO; this is display
    only. Falls back to the raw stored value for anything that isn't a
    clean ISO date, and to "" for a missing value. Usable as a Jinja
    filter: {{ contact.date_of_birth | format_date }}.
    """
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return text


# Chicago Manual of Style's month abbreviations -- May/June/July are
# already short enough that they're conventionally left unabbreviated;
# September is "Sept." (4 letters), not the 3-letter "Sep." strftime's
# %b would give. Used only by format_date_abbrev below, for the Hotel
# Room Type price/price-history date display Zeb specifically mocked up
# as "Sept. 05, 2026" -- format_date above (full month name, no period)
# stays the app-wide default everywhere else.
_ABBREV_MONTHS = [
    "Jan.", "Feb.", "Mar.", "Apr.", "May", "June",
    "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec.",
]


def format_date_abbrev(value):
    """Render a stored 'YYYY-MM-DD' date as 'Mon. DD, YYYY' (Chicago-style
    abbreviated month + period, e.g. '2026-09-05' -> 'Sept. 05, 2026') --
    the exact display format on the Edit Room Type mock (Zeb, Sept 2026:
    "Date display on form must be explicit in the format on the Mock
    Form"). Falls back to the raw stored value for anything that isn't a
    clean ISO date, and to "" for a missing value. Usable as a Jinja
    filter: {{ room.price_as_of | format_date_abbrev }}.
    """
    text = _clean(value)
    if not text:
        return ""
    try:
        d = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return text
    return f"{_ABBREV_MONTHS[d.month - 1]} {d.day:02d}, {d.year}"


def format_price(value, currency="$"):
    """Render a stored price as '$100.00' -- 2 decimal places, thousands
    separator, currency symbol prefixed. Returns "" for a missing/blank
    value rather than "$0.00", so an unset Room Type price shows as an
    empty cell, not a misleading zero. Usable as a Jinja filter:
    {{ room.price_per_night | format_price }}.
    """
    if value is None or value == "":
        return ""
    try:
        return f"{currency}{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def orblank(value):
    """A DB NULL becomes '' instead of Jinja printing the literal word
    "None" into an input's value="..." (the classic `{{ x.field if x else
    '' }}` gotcha — that ternary only guards against x itself being
    missing, not against x.field being NULL). Only guards against None
    specifically, so legitimate falsy values like 0 or False still render
    correctly. Usable as a Jinja filter: {{ item.field | orblank }}.
    """
    return "" if value is None else value


def basename(path):
    """Last path segment, splitting on both '/' and '\\' regardless of the
    host OS this process runs on (a Windows path saved by the app can be
    displayed correctly even when Flask itself happens to run elsewhere).
    """
    if not path:
        return ""
    return re.split(r"[\\/]", path)[-1] or path


def open_local_path(path):
    """Open a local file with whatever application the OS has registered
    for its file type — os.startfile on Windows (the target platform for
    this app), with a best-effort fallback for other OSes."""
    if hasattr(os, "startfile"):
        os.startfile(path)  # Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def pick_file_dialog():
    """Pop a native OS file-picker dialog and return (path, error). Works
    because the Flask process runs locally on the same machine as the
    browser (per the app's single-user, locally-run design) — a web page
    itself can never see a real local filesystem path, only a native
    dialog invoked server-side can."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None, "The file browser isn't available in this environment — type or paste the path instead."
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="Select a file")
        root.destroy()
    except Exception as e:
        return None, f"Couldn't open the file browser ({e}) — type or paste the path instead."
    return (path or None), None


def pick_files_dialog(title="Select files", filetypes=None):
    """Like pick_file_dialog, but lets the user multi-select any number of
    files in one go (Ctrl/Shift-click, or drag a selection box) — used by
    the Supplier Documents "Bulk Import Images" feature so a whole folder
    of photos can be picked at once instead of one Browse… per file.
    Returns (list_of_paths, error); list_of_paths is [] (not None) when
    nothing was picked or the dialog was cancelled, so callers can always
    iterate the result without a None-check."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return [], "The file browser isn't available in this environment — add files individually instead."
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(title=title, filetypes=filetypes or [("All files", "*.*")])
        root.destroy()
    except Exception as e:
        return [], f"Couldn't open the file browser ({e}) — add files individually instead."
    return list(paths), None
