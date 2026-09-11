# TMS in-app help content

This folder is the source of everything shown in the in-app Help panel —
the "Help" button in the top-right corner of every signed-in page (see
`blueprints/help.py`). It is **not** served as a help page itself (only
files inside a section subfolder are); it's just documentation for
whoever edits this content next.

Ported from "Book to Movie" studio's identical `help_content/` (Zeb:
"create a 'Help Module' for TMS exactly following the pattern of the Help
features in [that] project ... with a tree structure").

## Adding a page to an existing section

Drop a `<page-name>.md` file into the section's folder, with front matter
at the top:

```markdown
---
title: Human-Readable Title
order: 2
keywords: [a, few, search, terms]
---
Your Markdown content starts here.
```

- `title` — shown in the tree and as the page heading. Optional; falls
  back to the file name, title-cased.
- `order` — controls position within the section (lower first, ties break
  alphabetically by title). Optional; defaults to last.
- `keywords` — extra terms the drawer's search box matches against,
  beyond the title itself. Optional.

The page's `help_id` — what a page's `help_id=` kwarg (see below) links to
— is always `<section-folder-name>/<file-name-without-.md>`. There's
nothing else to register; it appears in the tree automatically on the
next request.

## Adding a new section

Create a new folder here. Optionally add a `_section.json` inside it:

```json
{"title": "Human-Readable Section Title", "order": 21}
```

Both fields are optional — without one, the folder name is title-cased
and it sorts after every explicitly-ordered section.

## Making the "Help" button open to a specific page

TMS has one persistent Help button (top-right corner, every signed-in
page — see `templates/base.html`). Unlike the source project, most pages
don't need to do anything special to make it point at the right section:
`base.html` derives the default `data-help-id` straight from
`request.blueprint` (`"<blueprint>/overview"`), the same value that
already drives the sidebar's active-link highlighting — so a section
folder named after a blueprint (`suppliers`, `poi`, `table_maintenance`,
...) with an `overview.md` inside it is picked up automatically, with no
route changes needed.

A page can still override the default explicitly, the same way the
source project's every route passes `help_id=` to `render_template()`:

```python
return render_template("some_page.html", help_id="suppliers/documents")
```

Use this when a page wants the drawer to open to a *different* page than
its blueprint's default overview (for example, a page under `suppliers`
that's really about the Documents, Links and Images sub-module could open
straight to `suppliers/documents` instead of `suppliers/overview`).

A page that renders before login, or a blueprint with no matching help
section at all, just falls back to the tree's first page — `static/js/
help.js`'s `openFirstAvailablePage` handles that, nothing breaks.

**If TMS ever grows a page with several tabs under one URL** (the way the
source project's `workspace.html` has Chapters/Premise/Costs/... under a
single project URL), that page's own script can call
`TMSHelp.setHelpId(...)` to keep the Help button's `data-help-id` in sync
as the visible tab changes, without a page reload — see
`static/js/help.js`'s `setHelpId` for the hook, and the source project's
own `workspace.html` for a worked example of the pattern (its
`HELP_ID_BY_TAB` map inside `selectTab()`). No page in TMS needs this yet.

## The full printable manual

`blueprints/help.py` also keeps the older, simpler shape from before this
tree existed: `/help` opens `Help/TMS-User-Manual.pdf` (relative to the
app root) in the OS's default viewer, the same as double-clicking it in
Explorer. No PDF ships with this change — the route reports a clear
"file not found" message until one is dropped into the `Help/` folder, the
same graceful degradation the source project's version has for a missing
file. `getting-started/welcome.md` links to it.

## What NOT to do here

- Don't nest a section folder inside another section folder — only one
  level of folders is walked.
- Don't rely on HTML inside these Markdown files being sanitized — it
  isn't (these files are trusted, git-controlled content, not end-user
  input), so don't paste in untrusted Markdown from outside the team.
- Keep pages to the Markdown this app's renderer actually supports: `#`/
  `##`/`###` headers, paragraphs, `- ` and `1. ` lists (with indented
  continuation lines for a wrapped item), **bold**, *italic*, `` `code` ``,
  fenced ``` code blocks, and `[text](url)` links. There's no table or
  blockquote support, and no nested lists — `blueprints/help.py`'s
  `_render_markdown` is a small, dependency-free renderer (this app avoids
  adding new PyPI dependencies where it reasonably can — see
  `requirements.txt`), not a full Markdown implementation.
