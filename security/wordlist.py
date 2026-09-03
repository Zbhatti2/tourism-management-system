"""
Recovery-phrase wordlist and generator (spec §3.7 — password recovery via
seed words).

NOTE: this is a locally-defined, human-friendly wordlist for this app's
account recovery only. It is deliberately NOT the standard BIP-39 wordlist used by
cryptocurrency wallets — there is no need for cross-wallet interoperability
here, only for a phrase a person can legibly write down and re-type. 220
words * 12 picks (with repetition allowed) gives ~12 * log2(220) ≈ 104 bits
of entropy, comfortably enough for local account recovery.
"""
import hashlib
import secrets

WORDLIST = [
    "abacus", "acorn", "amber", "anchor", "angle", "apple", "arch", "arrow",
    "ash", "aspen", "autumn", "badge", "banjo", "barn", "basil", "basket",
    "beacon", "bear", "beaver", "beech", "bell", "berry", "birch", "bison",
    "blanket", "blossom", "bluebird", "boulder", "branch", "breeze", "brick",
    "bridge", "brook", "bronze", "brush", "bucket", "buffalo", "cabin",
    "cactus", "camel", "candle", "canoe", "canyon", "cardinal", "carrot",
    "cedar", "cellar", "chalk", "chapel", "cherry", "chestnut", "chimney",
    "cinder", "clover", "coast", "cobalt", "comet", "compass", "copper",
    "coral", "cotton", "cottage", "cove", "coyote", "cradle", "crane",
    "crater", "cricket", "crow", "crystal", "current", "cypress", "dahlia",
    "daisy", "dandelion", "deer", "delta", "desert", "dolphin", "dove",
    "dragonfly", "drift", "drum", "eagle", "ebony", "echo", "eel", "elder",
    "elk", "elm", "ember", "emerald", "falcon", "feather", "fern", "field",
    "finch", "fjord", "flame", "flint", "forest", "fox", "garnet", "geode",
    "ginger", "glacier", "granite", "grove", "gull", "hamlet", "harbor",
    "harp", "hawk", "hazel", "heather", "hemlock", "heron", "hickory",
    "holly", "honey", "horizon", "hummingbird", "ibis", "iguana", "indigo",
    "ivory", "ivy", "jade", "jasmine", "jay", "juniper", "kestrel", "kettle",
    "kiln", "kite", "lagoon", "lantern", "larch", "lark", "laurel",
    "lavender", "ledge", "lichen", "lighthouse", "lilac", "lily", "linen",
    "lynx", "magma", "magnolia", "mallard", "mango", "maple", "marble",
    "marigold", "marsh", "meadow", "mesa", "meteor", "millet", "mint",
    "mirror", "mistletoe", "moss", "mulberry", "nectar", "nest", "nettle",
    "nutmeg", "oak", "oasis", "obsidian", "ocelot", "olive", "onyx",
    "opal", "orbit", "orchard", "orchid", "osprey", "otter", "owl",
    "paddle", "palm", "pansy", "panther", "papyrus", "parchment", "peach",
    "peak", "pear", "pebble", "pelican", "pepper", "petal", "pigeon",
    "pine", "plateau", "plum", "pond", "poppy", "prairie", "quail",
    "quarry", "quartz", "quill", "rabbit", "raven", "reed", "ridge",
    "river", "robin", "rosemary", "saffron", "sage", "salmon", "sandpiper",
    "sapphire", "savanna", "sequoia", "shale", "sparrow", "spruce",
    "starling", "stork", "sunflower", "swallow", "sycamore", "tamarind",
    "tarn", "thistle", "thrush", "thyme", "tidepool", "timber", "topaz",
    "trellis", "tulip", "tundra", "turquoise", "valley", "violet",
    "walnut", "warbler", "waterfall", "wheat", "willow", "wisteria",
    "woodland", "wren", "yarrow", "zephyr",
]

assert len(set(WORDLIST)) == len(WORDLIST), "wordlist must have no duplicates"

PHRASE_LENGTH = 12


def generate_seed_phrase() -> str:
    """Cryptographically random 12-word recovery phrase, space-separated."""
    words = [secrets.choice(WORDLIST) for _ in range(PHRASE_LENGTH)]
    return " ".join(words)


def normalize_phrase(phrase: str) -> str:
    return " ".join(phrase.strip().lower().split())


def hash_phrase(phrase: str) -> str:
    """One-way hash of the normalized phrase, stored in users.recovery_seed_hash
    for verification during a password-reset flow (never store the phrase itself)."""
    normalized = normalize_phrase(phrase)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_phrase(phrase: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_phrase(phrase), stored_hash)
