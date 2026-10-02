"""The reading view the connector sends: the same content as the file on disk, minus repetition.

The files in the library are written for people and tools (front matter, links to saved images, a reminder
on every visual page). Sent to an AI over and over, that repetition costs tokens without adding anything, so
this strips only what is repeated or machine-only. Every sentence, number, label, table, caption,
description and locator stays exactly as it is.
"""

from __future__ import annotations

import re

from .util import parse_front_matter

DROP_KEYS = {"size", "notes", "visual_content", "source_file", "synced"}
DATE_KEYS = {"due", "posted", "canvas_updated", "updated", "start", "unlock"}
GENERIC_NOTE = re.compile(r"^> Some pages or slides are visual \(see the '> Visual content' markers\)\.[^\n]*\n?", re.M)
VISUAL_LINE = re.compile(r"^> Visual content: (.+?)\. View (?:the (?:page|slide) image for exact details|it directly)\.[ \t]*$", re.M)
# Pictures saved from slides: the AI looks at those with view_page(deck, slide), so the file path isn't needed.
# (Lecture screenshots and Word figures keep their paths: that's how they're opened.)
ASSET_LINK = re.compile(r"\[(Slide \d+ image \d+[^\]\n]*)\]\((?:[^)\s]*\.assets/[^)\s]+)\)")
ORIGINAL_LINE = re.compile(r"^Original file: [^\n]+\n", re.M)


def for_index(body: str) -> str:
    """The text that gets searched: the document's words without the file plumbing, so boilerplate never
    shows up as a search hit."""
    body = GENERIC_NOTE.sub("", body)
    body = ORIGINAL_LINE.sub("", body)
    body = VISUAL_LINE.sub(lambda m: f"> Visual: {m.group(1)}", body)
    body = ASSET_LINK.sub(lambda m: f"[{m.group(1)}]", body)
    return re.sub(r"\n{3,}", "\n\n", body)


def compact(text: str) -> str:
    meta, body = parse_front_matter(text)
    if not meta:
        body = text
    header = []
    empty = "_No text found in this file._" in body or "_Couldn't extract text" in body or len(body) < 400
    for key, value in meta.items():
        value = str(value or "").strip()
        if (key in DROP_KEYS and not (key == "notes" and empty)) or not value:   # keep why a file has no text
            continue
        if key == "date" and DATE_KEYS & set(meta):   # an ISO copy of a date already given in words
            continue
        if key == "title" and re.search(r"^# " + re.escape(value), body, re.M):
            continue
        if value in body:            # already stated in the document itself
            continue
        header.append(f"{key.replace('_', ' ')}: {value}")
    body = GENERIC_NOTE.sub("", body)
    body = ORIGINAL_LINE.sub("", body)
    body = VISUAL_LINE.sub(lambda m: f"> Visual: {m.group(1)} (view_page)", body)
    body = ASSET_LINK.sub(lambda m: f"[{m.group(1)}]", body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return (f"({'; '.join(header)})\n\n" if header else "") + body + "\n"
