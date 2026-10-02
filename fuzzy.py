"""
Fuzzy name matching for duplicate checks (import wizard and Find duplicates).

Three levels, strongest first:

1. normalize(): the same name once case, accents, punctuation and filler
   words are ignored -- "The Avari Hotel, Lahore" == "avari lahore".
2. sound_key(): a "sounds like" key for names transliterated from Urdu,
   Arabic, Persian and Chinese into English, where the same name is spelled
   many ways. Common spelling variants are folded together (q->k, ee->i,
   ph->f, w->v, double letters...), then vowels after the first letter are
   dropped, so Ahmad / Ahmed -> "ahmd", Muhammad / Mohammed -> "mhmd",
   Qila / Kila -> "kl", Masjid / Masjed -> "msjd".
3. similarity(): a 0..1 closeness score (character sequence + shared words)
   for near misses the key doesn't catch, e.g. a dropped word.

Plus a distance check (utils.haversine_km) for "same place, different
name" -- applied by the callers, which know the coordinates.
"""
import re
import unicodedata
from difflib import SequenceMatcher

# Words that don't distinguish one place from another. Kept short on
# purpose: "hotel" in "Hotel One" vs "One" is noise, but "fort" in
# "Lahore Fort" vs "Lahore Museum" is not, so place-type words stay.
FILLER = {"the", "a", "an", "of", "and", "at", "in", "e", "al", "el",
          "hotel", "hotels", "restaurant", "inn", "guesthouse", "guest", "house",
          "embassy", "consulate", "mission", "station", "railway", "airport", "international"}

# Local words and their English equivalents, so "Badshahi Masjid" matches
# "Badshahi Mosque" and "Shahi Qila" matches "Shahi Fort".
SYNONYMS = {"masjid": "mosque", "masjed": "mosque", "musjid": "mosque", "qila": "fort", "qilla": "fort",
            "kila": "fort", "killa": "fort", "qal'a": "fort", "bagh": "garden", "gardens": "garden",
            "baagh": "garden", "mazar": "tomb", "maqbara": "tomb", "makbara": "tomb", "dargah": "shrine",
            "mandir": "temple", "gurudwara": "gurdwara", "gurdwaara": "gurdwara",
            "bazar": "bazaar", "jheel": "lake", "jhil": "lake", "darya": "river", "pul": "bridge",
            "mahal": "palace", "haveli": "mansion", "cantt": "cantonment", "intl": "international", "jn": "junction",
            "jct": "junction", "rd": "road", "st": "street", "mt": "mount"}

# Spelling variants common in transliterated South-Asian / Arabic / Chinese
# names. Applied in order to the normalized text.
_FOLDS = [
    (r"ph", "f"), (r"q", "k"), (r"ck", "k"), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"x", "ks"),
    (r"w", "v"), (r"z", "j"), (r"dh", "d"), (r"th", "t"), (r"gh", "g"), (r"kh", "k"), (r"bh", "b"),
    (r"ee", "i"), (r"ea", "i"), (r"oo", "u"), (r"ou", "u"), (r"y\b", "i"), (r"ie\b", "i"),
]


def strip_accents(text):
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def normalize(name, drop_filler=True):
    """Lower-case, accent-free, punctuation-free, single-spaced; filler words
    removed (unless that would leave nothing)."""
    text = strip_accents((name or "").lower())
    text = re.sub(r"[’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if not drop_filler:
        return text
    words = [SYNONYMS.get(w, w) for w in text.split()]
    kept = [w for w in words if w not in FILLER]
    return " ".join(kept) if kept else " ".join(words)


def _fold(word):
    for pat, rep in _FOLDS:
        word = re.sub(pat, rep, word)
    return re.sub(r"(.)\1+", r"\1", word)  # collapse doubled letters


def sound_key(name):
    """'Sounds like' key: each word folded, then its vowels (after the first
    letter) and 'h' dropped -- so the key keeps the consonant skeleton."""
    keys = []
    for word in normalize(name).split():
        if word.isdigit():
            keys.append(word)
            continue
        w = _fold(word)
        skeleton = w[0] + re.sub(r"[aeiouh]", "", w[1:])
        keys.append(re.sub(r"(.)\1+", r"\1", skeleton))
    return " ".join(keys)


def similarity(a, b):
    """0..1 closeness of two names after normalize(): the better of a
    character-sequence ratio and a shared-word (token set) ratio."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    common = ta & tb
    token = (2 * len(common) / (len(ta) + len(tb))) if (ta and tb) else 0.0
    # every word of a 2+ word name inside the other ("Lahore Fort" / "Lahore Fort Shahi Qila")
    if common and ((common == ta and len(ta) >= 2) or (common == tb and len(tb) >= 2)):
        token = max(token, 0.9)
    return max(seq, token)


SIMILAR = 0.86       # similarity at or above this = possible duplicate
NEARBY_KM = 0.15     # same type within 150 m = possible duplicate, whatever the name


def _without(name, ignore):
    """name normalized, minus the ignored words (e.g. the city's own name,
    which many hotel and station names contain) -- unless that would leave
    nothing, in which case the full name is used."""
    words = normalize(name).split()
    kept = [w for w in words if w not in ignore]
    return " ".join(kept) if kept else " ".join(words)


def compare(a, b, alt_b=(), ignore=()):
    """How name `a` relates to name `b` (and b's alternate names):
    'same', 'sounds' (sounds-like key matches and spelled closely),
    'similar', or None. `ignore`: words to leave out of the comparison,
    normally the city's name ("Islamabad Hotel" vs "Islamabad Serena Hotel"
    are compared as "" vs "serena", not as near-identical)."""
    ignore = {w for w in (normalize(x) for x in ignore) for w in w.split()}
    names = _variants(b) + [v for x in alt_b if x for v in _variants(x)]
    best = None
    for va in _variants(a):
        how = _compare_one(va, names, ignore)
        if how == "same":
            return how
        if how and (best is None or ["similar", "sounds"].index(how) > ["similar", "sounds"].index(best)):
            best = how
    return best


def _variants(name):
    """A name plus, for "Lahore Fort (Shahi Qila)", the parts outside and
    inside the brackets -- either may be how someone else wrote it."""
    out = [name]
    m = re.match(r"^(.*?)\((.*?)\)(.*)$", name or "")
    if m:
        outside = (m.group(1) + " " + m.group(3)).strip()
        out += [x for x in (outside, m.group(2).strip()) if x]
    return out


def _words_close(a, b, minimum=0.7):
    """Sounds-alike also needs every differing word to be a near spelling of
    its counterpart (Ahmad / Ahmed 0.8, Qila / Kila 0.75) -- this stops two
    different words that happen to share a consonant skeleton
    (Serena / Shrine) from matching."""
    wa, wb = normalize(a).split(), normalize(b).split()
    if len(wa) != len(wb):
        return True  # key equality already lined the words up; leave it to the overall score
    return all(x == y or SequenceMatcher(None, x, y).ratio() >= minimum for x, y in zip(wa, wb))


def _compare_one(a, names, ignore):
    if any(normalize(a) and normalize(a) == normalize(n) for n in names):
        return "same"
    sa = _without(a, ignore)
    for n in names:
        sn = _without(n, ignore)
        if sa == sn:
            return "same"
        # "Sounds like" also needs the spellings to be close, so a short key
        # can't collide by accident (Daud / Dadu).
        if sound_key(sa) and sound_key(sa) == sound_key(sn) and similarity(sa, sn) >= 0.7 and _words_close(sa, sn):
            return "sounds"
    if any(similarity(sa, _without(n, ignore)) >= SIMILAR for n in names):
        return "similar"
    return None


def split_alt_names(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
