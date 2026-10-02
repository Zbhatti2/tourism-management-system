"""
AI Agents — extraction helpers.

Two jobs: fetching a Source URL's readable text (fetch_source_text), and
calling the extraction model to propose field values from that text
(extract_fields_with_model). Kept as its own module — same pattern as
address_parsing.py / knowledge_graph.py — so blueprints/ai_agents.py's
routes stay about the Agent Run / Review Queue pipeline, not extraction
mechanics.

Both functions import their third-party dependency (requests, anthropic)
LAZILY, inside the function body, and turn a missing package into a plain
SourceFetchError/ExtractionError rather than an ImportError. This matters
because this module is imported by blueprints/ai_agents.py, which is
imported by app.py's create_app() — i.e. by every single page in TMS, not
just the AI Agents module. TMS's live deployment can't have its Windows
venv `pip install`ed remotely (see requirements.txt's note), so there will
always be a window where this code has shipped but `requests`/`anthropic`
haven't been installed yet. A hard top-level import would break the whole
app in that window; a lazy one only affects the one button that needs it.

Both functions are also the ONLY places this module touches the network —
tests monkeypatch fetch_source_text/extract_fields_with_model directly
(or, for extract_fields_with_model's own parsing logic, stub out the
`anthropic` module) rather than hitting a real URL or a real API key, so
the rest of the pipeline is fully testable offline.
"""
import base64
import re
from html.parser import HTMLParser

from config import Config


class SourceFetchError(Exception):
    """A Source URL could not be read. Callers decide whether one failed
    source should abort the whole run or just be skipped/noted."""


class ExtractionError(Exception):
    """The extraction model call itself failed, or isn't configured."""


class _TextExtractor(HTMLParser):
    """Very small HTML -> plain text reducer: strips tags/scripts/styles,
    keeps everything else, collapses whitespace. Not a full renderer (no
    layout, no tables-as-grids) — good enough for feeding a page's visible
    text to the model, which is all extraction needs."""
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text)

    def text(self):
        return "\n".join(self.chunks)


def fetch_source_text(url, max_chars=12000, timeout=20):
    """Fetches a URL and returns cleaned, truncated plain text. Raises
    SourceFetchError on any failure — bad status, timeout, non-text
    content, no 'requests' package installed, etc."""
    try:
        import requests
    except ImportError as e:
        raise SourceFetchError(
            "The 'requests' package isn't installed in this app's Python environment — "
            "run `pip install -r requirements.txt` and restart TMS."
        ) from e

    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TMS-AI-Agent/1.0)"},
        )
    except requests.RequestException as e:
        raise SourceFetchError(f"Could not reach {url}: {e}") from e

    if resp.status_code >= 400:
        raise SourceFetchError(f"{url} returned HTTP {resp.status_code}")

    content_type = resp.headers.get("Content-Type", "")
    if "text" not in content_type and "html" not in content_type:
        raise SourceFetchError(f"{url} is not a text/HTML document ({content_type or 'unknown type'})")

    parser = _TextExtractor()
    try:
        parser.feed(resp.text)
    except Exception as e:
        raise SourceFetchError(f"Could not parse {url}: {e}") from e

    text = re.sub(r"\n{3,}", "\n\n", parser.text()).strip()
    if not text:
        raise SourceFetchError(f"{url} had no readable text content")
    return text[:max_chars]


def extract_fields_with_model(entity_short_label, scope_label, field_specs, sources, images=None, on_usage=None):
    """entity_short_label: e.g. "Hotel". scope_label: the Agent Run's
    scope, e.g. "Avari Hotel, Lahore". field_specs: the exact whitelist —
    a list of (field_name, field_label) — the model is constrained to;
    see blueprints/ai_agents.py's ENTITY_FIELDS. sources: list of
    (url, text) tuples, already fetched. images: optional list of dicts
    {"label", "mime_type", "data"} (raw image bytes, e.g. a hotel-booking
    screenshot Zeb uploaded as an Agent Run Source) — sent as vision input
    alongside any URL text, so the model can read a screenshot the same
    way it reads a fetched web page. Pass None/[] for a text-only run.

    Returns a list of dicts: field_name, proposed_value, confidence
    (float 0-1 or None), source_citation (str or None) — one per field the
    model found support for in the given text/images. Raises
    ExtractionError if the API key is missing, the 'anthropic' package
    isn't installed, the call fails, or the model returns nothing usable.

    on_usage: optional callback(model, usage) for AI usage metering
    (ai_usage.record), called once the API has answered."""
    if not Config.ANTHROPIC_API_KEY:
        raise ExtractionError(
            "No ANTHROPIC_API_KEY is configured — copy .env.example to .env in the App "
            "folder, add your key, and restart TMS."
        )

    try:
        import anthropic
    except ImportError as e:
        raise ExtractionError(
            "The 'anthropic' package isn't installed in this app's Python environment — "
            "run `pip install -r requirements.txt` and restart TMS."
        ) from e

    images = images or []
    field_names = [name for name, _ in field_specs]
    field_list = "\n".join(f"- {name}: {label}" for name, label in field_specs)
    source_block = "\n\n".join(f"--- Source: {url} ---\n{text}" for url, text in sources)
    images_note = (
        f"\n\n{len(images)} image source(s) are also attached below, each preceded by a text "
        f"label identifying it — read them the same way you would a fetched web page."
        if images else ""
    )
    prompt = (
        f"You are helping populate a Tourism Management System record for this "
        f"{entity_short_label}: \"{scope_label}\".\n\n"
        f"Only propose values for these fields (use the exact field_name shown below, "
        f"never invent a new one):\n{field_list}\n\n"
        f"Base every value ONLY on the source text/images below — never use outside "
        f"knowledge, and never guess. Skip any field the sources don't actually support. For "
        f"each field you do propose, give a confidence from 0 (uncertain) to 1 (explicitly "
        f"and clearly stated) and cite which source it came from (a source URL, or an image "
        f"by its label).{images_note}\n\n{source_block}"
    )

    # Text first, then each image preceded by its own "Image: <label>" text
    # block so the model can cite it by name in source_citation — order
    # doesn't matter to the API, but keeping the instructions together up
    # front reads more reliably than interleaving them with image blocks.
    content = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "text", "text": f"Image: {img['label']}"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["mime_type"],
                "data": base64.b64encode(img["data"]).decode("ascii"),
            },
        })

    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=Config.ANTHROPIC_MODEL,
            max_tokens=2000,
            tools=[{
                "name": "propose_fields",
                "description": "Propose field values extracted from the provided source text/images.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field_name": {"type": "string", "enum": field_names},
                                    "proposed_value": {"type": "string"},
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                    "source_citation": {"type": "string"},
                                },
                                "required": ["field_name", "proposed_value"],
                            },
                        },
                    },
                    "required": ["fields"],
                },
            }],
            tool_choice={"type": "tool", "name": "propose_fields"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        raise ExtractionError(f"The extraction model call failed: {e}") from e

    if on_usage is not None:  # AI usage metering (ai_usage.py) -- called before any parsing can fail
        on_usage(Config.ANTHROPIC_MODEL, getattr(response, "usage", None))
    tool_use = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise ExtractionError("The extraction model didn't return any structured output.")
    raw_fields = tool_use.input.get("fields", [])

    results = []
    for f in raw_fields:
        name = f.get("field_name")
        if name not in field_names:
            continue  # defense in depth -- never trust an out-of-whitelist name, even though the schema constrains it
        value = (f.get("proposed_value") or "").strip()
        if not value:
            continue
        confidence = f.get("confidence")
        try:
            confidence = max(0.0, min(1.0, float(confidence))) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        results.append({
            "field_name": name,
            "proposed_value": value,
            "confidence": confidence,
            "source_citation": (f.get("source_citation") or "").strip() or None,
        })
    return results
