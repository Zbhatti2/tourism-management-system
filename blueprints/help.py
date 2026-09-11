"""Help.

Ported from the in-app Help module in "Book to Movie" studio
(webapp/routes/help.py) at Zeb's request — "create a 'Help Module' for TMS
exactly following the pattern of the Help features in [that] project ...
with a tree structure." Two generations of the same feature, same as the
source project:

1. The original shape: `/help` opens Help/TMS-User-Manual.pdf in the OS's
   default viewer (the same thing double-clicking it in Explorer does).
   Structured as HELP_TOPICS, a registry, so more topics could be added
   later — see the classes/dict below. No PDF ships with this change (TMS
   doesn't have a printable manual yet); the route degrades gracefully
   (a clear "file not found" page, not a crash) until one is dropped into
   the Help/ folder, exactly like a missing file behaves in the source
   project.

2. The in-app contextual Help panel: a section/page tree built from
   Markdown files under help_content/, rendered into the Offcanvas drawer
   that the "Help" button (fixed top-right corner, see templates/base.html)
   opens (see templates/_help_drawer.html and static/js/help.js), instead
   of that button navigating to #1 directly. Unlike the source project,
   which has each page pass a `help_id=` kwarg to render_template (the same
   convention as its `active_nav`), TMS's base.html already derives
   `active_nav`-equivalent sidebar highlighting straight from
   `request.blueprint` (see base.html) — so the Help button's default
   `data-help-id` does the same here (`<request.blueprint>/overview`,
   falling back to Getting Started for blueprints with no help section of
   their own yet). A page can still override it explicitly by passing
   `help_id=` to render_template, same as the source project, for the rare
   case for the auto-derived default isn't the right page.

Adding a help page: drop a `<page>.md` file under help_content/<section>/
with `title` (and optionally `order`, `keywords`) in a leading `---`
front-matter block; it appears in the tree automatically as
"<section-folder-name>/<file-name>" (its help_id). A new section is just a
new folder, optionally with a `_section.json` for {"title": ..., "order":
...}. See help_content/README.md for the full writeup.
"""

import json
import re
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template

from auth.decorators import login_required
from config import BASE_DIR
from utils import open_local_path

help_bp = Blueprint("help", __name__)

HELP_DIR = "Help"

# App/help_content/ -- sibling of this blueprints/ package's parent
# (blueprints/help.py -> blueprints/ -> App/), same as templates/ and
# static/.
CONTENT_DIR = Path(__file__).resolve().parent.parent / "help_content"


class HelpTopic:
    """One help document. A future topic could carry a `url` instead of a
    `filename` to point at external documentation."""

    def __init__(self, label: str, filename: str):
        self.label = label
        self.filename = filename

    @property
    def path(self):
        return BASE_DIR / HELP_DIR / self.filename


HELP_TOPICS = {
    "manual": HelpTopic(label="TMS User Manual", filename="TMS-User-Manual.pdf"),
}

DEFAULT_TOPIC = "manual"


def _open_topic(topic: HelpTopic):
    """-> (status, detail). Never raises: a missing file or a machine that
    can't open it is an ordinary outcome to report, not a 500."""
    path = topic.path

    if not path.is_file():
        return "missing", (
            f"Help file not found — add {topic.filename} to the "
            f"{HELP_DIR} folder."
        )

    try:
        open_local_path(str(path))
    except Exception as e:
        return "error", f"Could not open {topic.filename}: {e}"

    return "opened", f"Opening {topic.filename} in your default PDF viewer."


@help_bp.route("/help")
@login_required
def open_help():
    topic = HELP_TOPICS[DEFAULT_TOPIC]
    status, detail = _open_topic(topic)
    return render_template(
        "help.html",
        status=status,
        detail=detail,
        label=topic.label,
        path=topic.path,
    ), (200 if status == "opened" else 404 if status == "missing" else 500)


# ---------------------------------------------------------------------------
# In-app contextual help panel (tree + Markdown pages)
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?(.*)$", re.DOTALL)


def _parse_front_matter(text):
    """Split a help page's leading `---`-delimited front matter from its
    Markdown body. Front matter is flat `key: value` lines, with
    `key: [a, b, c]` for the one list-valued field (keywords) -- hand-rolled
    rather than pulling in a YAML dependency, since nothing here nests
    (same reasoning as _render_markdown below skipping the `markdown` PyPI
    package)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            value = value.strip("'\"")
        meta[key] = value
    return meta, m.group(2)


def _humanize(slug):
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_tree():
    """Walk help_content/ and build the section -> pages tree that drives
    both the drawer's sidebar and /help/page/<help_id> lookups. Re-walked
    on every call rather than cached at import time, so an edited .md file
    is live on the next request with no server restart -- this is a
    couple dozen small text files, not a hot path worth caching."""
    sections = []
    if not CONTENT_DIR.exists():
        return sections
    for section_dir in CONTENT_DIR.iterdir():
        if not section_dir.is_dir():
            continue
        meta_path = section_dir / "_section.json"
        section_meta = {}
        if meta_path.exists():
            try:
                section_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                section_meta = {}
        pages = []
        for md_path in section_dir.glob("*.md"):
            text = md_path.read_text(encoding="utf-8")
            page_meta, _ = _parse_front_matter(text)
            pages.append({
                "help_id": f"{section_dir.name}/{md_path.stem}",
                "title": page_meta.get("title") or _humanize(md_path.stem),
                "order": _safe_int(page_meta.get("order"), 999),
                "keywords": page_meta.get("keywords") or [],
            })
        if not pages:
            continue
        pages.sort(key=lambda p: (p["order"], p["title"]))
        sections.append({
            "key": section_dir.name,
            "title": section_meta.get("title") or _humanize(section_dir.name),
            "order": _safe_int(section_meta.get("order"), 999),
            "pages": pages,
        })
    sections.sort(key=lambda s: (s["order"], s["title"]))
    return sections


def _resolve_md_path(help_id):
    """help_id -> its .md file, refusing anything that would resolve outside
    help_content/ (help_id comes from the URL, so it's untrusted even though
    the files it ultimately points at are not)."""
    if not help_id or "/" not in help_id:
        return None
    section_key, _, page_key = help_id.partition("/")
    if not section_key or not page_key or "/" in page_key:
        return None
    candidate = CONTENT_DIR / section_key / f"{page_key}.md"
    try:
        resolved = candidate.resolve()
        resolved.relative_to(CONTENT_DIR.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


# --------------------------------------------------------- Markdown -> HTML

_HEADER_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_NUM_RE = re.compile(r"^\d+\.\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _inline(text):
    """Escape raw text, then layer in the handful of inline Markdown spans
    this app's help content actually uses. Escaping first (rather than
    building tags then escaping around them) means any stray '<'/'&' typed
    into a .md file is neutralized before any real tag is constructed --
    the same "escape early" ordering used for the rest of this app's
    user-entered text."""
    import html as _html
    text = _html.escape(text)
    text = _CODE_RE.sub(lambda m: "<code>" + m.group(1) + "</code>", text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def _render_markdown(text):
    """Small, dependency-free Markdown -> HTML renderer for help pages.

    Supports exactly what this app's help_content/*.md files use: h1-h3
    headers (#/##/###), paragraphs, bullet lists (- item) and numbered
    lists (1. item) with optional indented wrapped continuation lines,
    bold (**x**) and italic (*x*) spans, inline code (`x`), fenced code
    blocks (```), and [text](url) links. Deliberately hand-rolled rather
    than pulling in the `markdown` PyPI package (what the source project's
    help.py uses) -- this app runs from a plain venv on each tenant's own
    machine (requirements.txt is "kept intentionally minimal" on purpose),
    and there's no way to remotely `pip install` a new dependency onto an
    already-running installation. Same reasoning as _parse_front_matter's
    hand-rolled front matter parsing above. Not a general-purpose Markdown
    implementation -- just enough for hand-written help content.
    """
    lines = text.split("\n")
    html_parts = []
    para_buf = []
    list_state = None  # (tag, [items]) while inside a bullet/numbered list

    def flush_para():
        if para_buf:
            html_parts.append("<p>" + _inline(" ".join(para_buf)) + "</p>")
            para_buf.clear()

    def flush_list():
        nonlocal list_state
        if list_state:
            tag, items = list_state
            html_parts.append(
                f"<{tag}>" + "".join("<li>" + _inline(it) + "</li>" for it in items) + f"</{tag}>"
            )
            list_state = None

    i = 0
    while i < len(lines):
        line = lines[i]

        if _FENCE_RE.match(line):
            flush_para()
            flush_list()
            code_lines = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            import html as _html
            html_parts.append("<pre><code>" + _html.escape("\n".join(code_lines)) + "</code></pre>")
            i += 1  # skip the closing fence
            continue

        m = _HEADER_RE.match(line)
        if m:
            flush_para()
            flush_list()
            level = len(m.group(1))
            html_parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            flush_para()
            if not list_state or list_state[0] != "ul":
                flush_list()
                list_state = ("ul", [])
            list_state[1].append(m.group(1))
            i += 1
            continue

        m = _NUM_RE.match(line)
        if m:
            flush_para()
            if not list_state or list_state[0] != "ol":
                flush_list()
                list_state = ("ol", [])
            list_state[1].append(m.group(1))
            i += 1
            continue

        if not line.strip():
            flush_para()
            flush_list()
            i += 1
            continue

        if list_state and line.startswith("  "):
            # An indented continuation line, wrapping the current list item.
            list_state[1][-1] += " " + line.strip()
        else:
            para_buf.append(line.strip())
        i += 1

    flush_para()
    flush_list()
    return "\n".join(html_parts)


@help_bp.route("/help/tree")
@login_required
def help_tree():
    """The full section -> pages tree, fetched once by the drawer and
    cached client-side for the rest of the page's lifetime."""
    return jsonify({"sections": _load_tree()})


@help_bp.route("/help/page/<path:help_id>")
@login_required
def help_page(help_id):
    """One rendered help page, plus enough about its place in the tree
    (section key/title) for the drawer to expand the right branch and show
    a breadcrumb without re-deriving it from the full tree."""
    md_path = _resolve_md_path(help_id)
    if md_path is None:
        abort(404)
    text = md_path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(text)
    html = _render_markdown(body)

    section_key = help_id.split("/", 1)[0]
    sections = _load_tree()
    section_title = next((s["title"] for s in sections if s["key"] == section_key), _humanize(section_key))

    return jsonify({
        "help_id": help_id,
        "title": meta.get("title") or _humanize(md_path.stem),
        "section_key": section_key,
        "section_title": section_title,
        "html": html,
    })
