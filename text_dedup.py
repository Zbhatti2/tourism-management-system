"""
Spotting text a POI already has (Zeb, Oct 2026: "the Agents must be able to
identify duplication of text").

New text about a POI (from a PDF, an import, an agent) is checked against
what the POI already holds, cheapest first:

1. Wording: the text is split into sentences; a new sentence that closely
   matches one already stored (same words, give or take punctuation, case
   and small edits) is dropped. If nothing is left, the text is a
   duplicate and nothing is proposed.
2. Meaning: what survives is given to Claude with the stored text, which
   says whether it is
     - "duplicate": the same facts in other words -> dropped;
     - "adds":      new facts -> only those sentences are kept;
     - "conflicts": it disagrees with the stored text (a different year,
       name, place) -> kept, with the disagreements listed for the
       reviewer.
   (Step 2 is skipped when the POI has no stored text: everything is new.)
"""
import json
import re
from difflib import SequenceMatcher

SAME_SENTENCE = 0.85  # word-level similarity at which two sentences are the same


def sentences(text):
    """Sentences of a text (also split on line breaks and bullets)."""
    text = re.sub(r"\s+", " ", (text or "").replace("\r", "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\s*[\n•]\s*", text)
    return [p.strip() for p in parts if len(p.strip()) > 2]


def _words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def sentence_similarity(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    seq = SequenceMatcher(None, wa, wb).ratio()
    # A short sentence wholly inside a longer one is a repeat too.
    sa, sb = set(wa), set(wb)
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    contained = len(small & big) / len(small) if len(small) >= 5 else 0
    return max(seq, contained)


def new_sentences(existing, new, threshold=SAME_SENTENCE):
    """The sentences of `new` that `existing` doesn't already have (in
    wording)."""
    old = sentences(existing)
    out = []
    for s in sentences(new):
        if any(sentence_similarity(s, o) >= threshold for o in old):
            continue
        if any(sentence_similarity(s, o) >= threshold for o in out):  # repeated within the new text
            continue
        out.append(s)
    return out


def wording_check(existing, new):
    """(left-over text, share of the new text already present 0..1)."""
    all_new = sentences(new)
    left = new_sentences(existing, new)
    share = 1 - (len(left) / len(all_new)) if all_new else 1.0
    return " ".join(left), share


TOOL = {"name": "report_dedup", "description": "For each item: is the new text already covered by the stored text?",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {
                "key": {"type": "string"},
                "verdict": {"type": "string", "enum": ["duplicate", "adds", "conflicts"]},
                "new_text": {"type": "string",
                             "description": "Only the sentences of the new text that add facts, as written; empty "
                                            "for a duplicate"},
                "conflicts": {"type": "array", "items": {"type": "string"},
                              "description": "Each disagreement, e.g. 'Founded 1534 (new) vs 1535 (stored)'"}},
            "required": ["key", "verdict"]}}}, "required": ["items"]}}


def meaning_check(client, model, items, meter, create=None):
    """items: [{key, name, existing, new}] -> {key: {verdict, new_text,
    conflicts}}. Items with no stored text are 'adds' without asking."""
    out, ask = {}, []
    for it in items:
        if not (it.get("existing") or "").strip():
            out[it["key"]] = {"verdict": "adds", "new_text": it["new"], "conflicts": []}
        else:
            ask.append(it)
    for i in range(0, len(ask), 12):
        batch = ask[i:i + 12]
        prompt = (
            "A tourism catalog already holds some text about each place below. For each item, compare the NEW text "
            "with the STORED text and decide:\n"
            "- duplicate: the new text says nothing the stored text doesn't already say (even in other words);\n"
            "- adds: it adds facts -- return only the sentences that add them, in the new text's own wording;\n"
            "- conflicts: it disagrees with the stored text on a fact (a date, name, number, place) -- return the "
            "sentences that add or disagree, and list each disagreement.\n"
            "Judge facts, not style.\n\n" + json.dumps(
                [{"key": it["key"], "place": it.get("name"), "stored": it["existing"][:6000], "new": it["new"][:6000]}
                 for it in batch], ensure_ascii=False, indent=1))
        call = create or (lambda **kw: client.messages.create(**kw))
        resp = call(model=model, max_tokens=8000, tools=[TOOL], tool_choice={"type": "tool", "name": "report_dedup"},
                    messages=[{"role": "user", "content": prompt}])
        meter(getattr(resp, "usage", None))
        got = {}
        for b in getattr(resp, "content", []) or []:
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "report_dedup":
                got = {x.get("key"): x for x in (b.input or {}).get("items") or []}
        for it in batch:
            r = got.get(it["key"])
            if r is None:  # no answer: keep the text, unchecked
                out[it["key"]] = {"verdict": "adds", "new_text": it["new"], "conflicts": [], "unchecked": True}
                continue
            out[it["key"]] = {"verdict": r.get("verdict") or "adds",
                              "new_text": (r.get("new_text") or "").strip() if r.get("verdict") != "duplicate" else "",
                              "conflicts": [c for c in r.get("conflicts") or [] if c]}
    return out
