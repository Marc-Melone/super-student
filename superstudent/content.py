"""Canvas HTML -> Markdown, collecting what the HTML points at (files, videos, links).

Links to other Canvas items become placeholder tokens like (canvas-file:123) that the
sync step rewrites into relative links to the local copies once everything is placed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString
from markdownify import MarkdownConverter

FILE_RE = re.compile(r"/(?:api/v1/)?(?:(?:courses|groups|users)/\d+/)?files/(\d+)(?=[/?#]|$)")
PAGE_RE = re.compile(r"/courses/(\d+)/(?:pages|wiki)/([^/?#]+)")
ASSIGN_RE = re.compile(r"/courses/(\d+)/assignments/(\d+)(?=[/?#]|$)")
QUIZ_RE = re.compile(r"/courses/(\d+)/quizzes/(\d+)(?=[/?#]|$)")
DISC_RE = re.compile(r"/courses/(\d+)/(?:discussion_topics|announcements)/(\d+)(?=[/?#]|$)")
MEDIA_ATT_RE = re.compile(r"/media_attachments(?:_iframe)?/(\d+)")
MEDIA_OBJ_RE = re.compile(r"(?:/media_objects(?:_iframe)?/|media_comment_)([0-9A-Za-z_]{3,}(?:-[0-9A-Za-z]+)?)")
YOUTUBE_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^#]*&)?v=|shorts/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
KNOWN_PLATFORMS = {
    "panopto": "Panopto", "kaltura": "Kaltura", "zoom.us": "Zoom", "vimeo": "Vimeo", "echo360": "Echo360",
    "mediasite": "Mediasite", "yuja": "YuJa", "instructuremedia": "Canvas Studio", "arc.instructure": "Canvas Studio",
    "docs.google": "Google Docs", "drive.google": "Google Drive", "sharepoint": "SharePoint", "onedrive": "OneDrive",
    "box.com": "Box", "gradescope": "Gradescope", "mheducation": "McGraw Hill", "pearson": "Pearson",
    "cengage": "Cengage", "wiley": "Wiley", "perusall": "Perusall", "hypothes.is": "Hypothesis",
    "external_tools": "Canvas external tool (LTI)", "lti": "Canvas external tool (LTI)",
}


@dataclass
class Refs:
    files: Dict[str, str] = field(default_factory=dict)          # file id -> link text
    media: List[Dict[str, str]] = field(default_factory=list)    # {"kind": att|obj, "id", "title"}
    youtube: Dict[str, str] = field(default_factory=dict)        # video id -> label
    links: List[Dict[str, str]] = field(default_factory=list)    # outside links {url, text}
    embeds: List[Dict[str, str]] = field(default_factory=list)   # embedded tools {url, platform, title}
    pages: Dict[str, str] = field(default_factory=dict)          # this course's page slug -> link text

    def to_json(self) -> Dict:
        return {"files": self.files, "media": self.media, "youtube": self.youtube,
                "links": self.links, "embeds": self.embeds, "pages": self.pages}

    @classmethod
    def from_json(cls, data: Optional[Dict]) -> "Refs":
        data = data or {}
        return cls(files=dict(data.get("files") or {}), media=list(data.get("media") or []),
                   youtube=dict(data.get("youtube") or {}), links=list(data.get("links") or []),
                   embeds=list(data.get("embeds") or []), pages=dict(data.get("pages") or {}))

    def merge(self, other: "Refs") -> None:
        for fid, label in other.files.items():
            self.files.setdefault(fid, label)
        self.media.extend(other.media)
        for vid, label in other.youtube.items():
            self.youtube.setdefault(vid, label)
        self.links.extend(other.links)
        self.embeds.extend(other.embeds)
        for slug, label in other.pages.items():
            self.pages.setdefault(slug, label)


def platform_name(url: str) -> str:
    host_path = url.lower()
    for key, name in KNOWN_PLATFORMS.items():
        if key in host_path:
            return name
    return urlparse(url).hostname or "external site"


class _Converter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags=None, **kwargs):  # keep alt text even if src is odd
        alt = (el.attrs.get("alt") or el.attrs.get("title") or "").strip()
        src = el.attrs.get("src") or ""
        if not src:
            return f"[image: {alt}]" if alt else ""
        return f"![{alt}]({src})"


def _latex_from(img) -> Optional[str]:
    classes = img.get("class") or []
    latex = img.get("data-equation-content")
    if not latex and ("equation_image" in classes or "/equation_images/" in (img.get("src") or "")):
        latex = img.get("title") or img.get("alt") or ""
        if latex.lower().startswith("latex:"):
            latex = latex[6:]
        if not latex and "/equation_images/" in (img.get("src") or ""):
            latex = unquote(unquote((img.get("src") or "").split("/equation_images/")[1].split("?")[0]))
    return latex.strip() if latex else None


def _expand_spans(soup, table) -> None:
    """Repeat merged cells (rowspan/colspan) so every row keeps its columns lined up in Markdown."""
    pending: Dict[Tuple[int, int], str] = {}
    rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    for r, row in enumerate(rows):
        col = 0
        cells = [c for c in row.find_all(["td", "th"], recursive=False)]

        def fill_before(anchor, kind: str) -> None:
            nonlocal col
            while (r, col) in pending:
                new = soup.new_tag(kind)
                new.string = pending.pop((r, col))
                if anchor is not None:
                    anchor.insert_before(new)
                else:
                    row.append(new)
                col += 1

        for cell in cells:
            fill_before(cell, cell.name)
            try:
                colspan = max(1, min(int(cell.get("colspan", 1)), 50))
                rowspan = max(1, min(int(cell.get("rowspan", 1)), 200))
            except (TypeError, ValueError):
                colspan = rowspan = 1
            text = cell.get_text(" ", strip=True)
            for attr in ("colspan", "rowspan"):
                cell.attrs.pop(attr, None)
            for extra in range(1, colspan):
                blank = soup.new_tag(cell.name)
                cell.insert_after(blank)
            for rr in range(1, rowspan):
                for cc in range(colspan):
                    pending[(r + rr, col + cc)] = text if cc == 0 else ""
            col += colspan
        fill_before(None, "td")
        for key in [k for k in pending if k[0] == r]:      # spans hanging past this row's last cell
            new = soup.new_tag("td")
            new.string = pending.pop(key)
            row.append(new)


def _prepare(soup) -> None:
    """Keep meaning that plain Markdown conversion would flatten away."""
    # Superscripts and subscripts carry the math: e<sup>-rT</sup> must not become "e-rT".
    for tag in soup.find_all(["sup", "sub"]):
        text = tag.get_text("", strip=True)
        if not text:
            tag.decompose()
            continue
        mark = "^" if tag.name == "sup" else "_"
        short = re.fullmatch(r"[A-Za-z0-9]{1,3}|[A-Za-z0-9*′'+\-]", text)
        tag.replace_with(NavigableString(f"{mark}{text}" if short else f"{mark}({text})"))
    # Merged table cells would otherwise shift the rest of the row under the wrong headings.
    for table in soup.find_all("table"):
        if table.find(["td", "th"], attrs={"rowspan": True}) or table.find(["td", "th"], attrs={"colspan": True}):
            _expand_spans(soup, table)
    # A link's hover title adds nothing for a reader and breaks local link rewriting.
    for a in soup.find_all("a"):
        a.attrs.pop("title", None)


def html_to_markdown(html: Optional[str], base_url: str, course_id: str = "") -> Tuple[str, Refs]:
    refs = Refs()
    if not html or not html.strip():
        return "", refs
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    _prepare(soup)

    # Math typed with Canvas's equation editor is stored as images; keep the LaTeX.
    for img in soup.find_all("img"):
        latex = _latex_from(img)
        if latex:
            img.replace_with(NavigableString(f" ${latex}$ "))
            continue
        src = img.get("src") or ""
        m = FILE_RE.search(src)
        if m:
            refs.files.setdefault(m.group(1), img.get("alt") or "image")
            img["src"] = f"canvas-file:{m.group(1)}"
        elif src and not src.startswith(("http", "data:")):
            img["src"] = urljoin(base_url + "/", src)

    for frame in soup.find_all(["iframe", "video", "audio", "embed", "object"]):
        src = frame.get("src") or frame.get("data") or ""
        if not src:
            source = frame.find("source")
            src = source.get("src") if source else ""
        title = (frame.get("title") or frame.get("aria-label") or "").strip()
        absolute = urljoin(base_url + "/", src) if src else ""
        placeholder = None
        m_att = MEDIA_ATT_RE.search(src)
        m_obj = MEDIA_OBJ_RE.search(src) or MEDIA_OBJ_RE.search(frame.get("data-media-id") or "")
        m_yt = YOUTUBE_RE.search(src)
        if m_att:
            refs.media.append({"kind": "att", "id": m_att.group(1), "title": title})
            placeholder = f"[Embedded Canvas video: {title or 'untitled'}](canvas-media:att-{m_att.group(1)})"
        elif m_obj and ("media_object" in src or frame.get("data-media-id")):
            refs.media.append({"kind": "obj", "id": m_obj.group(1), "title": title})
            placeholder = f"[Embedded Canvas video: {title or 'untitled'}](canvas-media:obj-{m_obj.group(1)})"
        elif m_yt:
            refs.youtube.setdefault(m_yt.group(1), title)
            placeholder = f"[YouTube video: {title or m_yt.group(1)}](canvas-youtube:{m_yt.group(1)})"
        elif m_file := FILE_RE.search(src):
            refs.files.setdefault(m_file.group(1), title or "embedded file")
            placeholder = f"[Embedded file: {title or 'file'}](canvas-file:{m_file.group(1)})"
        elif absolute:
            plat = platform_name(absolute)
            refs.embeds.append({"url": absolute, "platform": plat, "title": title})
            placeholder = f"[Embedded {plat} content (not reachable through the Canvas API): {title or absolute}]({absolute})"
        if placeholder:
            new = soup.new_tag("p")
            new.string = placeholder
            frame.replace_with(new)
        else:
            frame.decompose()

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        media_id = a.get("id") or ""
        if "media_comment" in " ".join(a.get("class") or []) or media_id.startswith("media_comment_"):
            m = MEDIA_OBJ_RE.search(media_id) or MEDIA_OBJ_RE.search(href)
            if m:
                refs.media.append({"kind": "obj", "id": m.group(1), "title": text})
                a.replace_with(NavigableString(f"[Canvas video: {text or 'untitled'}](canvas-media:obj-{m.group(1)})"))
                continue
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urljoin(base_url + "/", href)
        api_endpoint = a.get("data-api-endpoint") or ""
        same_host = urlparse(absolute).hostname == urlparse(base_url).hostname
        m = FILE_RE.search(api_endpoint) or (FILE_RE.search(absolute) if same_host else None)
        if m:
            refs.files.setdefault(m.group(1), text or "file")
            a["href"] = f"canvas-file:{m.group(1)}"
            continue
        if same_host:
            for regex, token in ((PAGE_RE, "canvas-page"), (ASSIGN_RE, "canvas-assignment"),
                                 (QUIZ_RE, "canvas-quiz"), (DISC_RE, "canvas-discussion")):
                m = regex.search(absolute)
                if m:
                    # Only items of this course become local links; another course's page stays a Canvas link.
                    ours = not course_id or m.group(1) == str(course_id)
                    a["href"] = f"{token}:{m.group(2)}" if ours else absolute
                    if ours and token == "canvas-page":
                        refs.pages.setdefault(unquote(m.group(2)), text)
                    break
            else:
                m_att = MEDIA_ATT_RE.search(absolute)
                if m_att:
                    refs.media.append({"kind": "att", "id": m_att.group(1), "title": text})
                    a["href"] = f"canvas-media:att-{m_att.group(1)}"
                elif "/external_tools/" in absolute:
                    refs.embeds.append({"url": absolute, "platform": "Canvas external tool (LTI)", "title": text})
                    a["href"] = absolute
                else:
                    a["href"] = absolute
            continue
        m_yt = YOUTUBE_RE.search(absolute)
        if m_yt:
            refs.youtube.setdefault(m_yt.group(1), text)
        elif absolute.startswith("http"):
            refs.links.append({"url": absolute, "text": text})
        a["href"] = absolute

    md = _Converter(heading_style="ATX", bullets="-", escape_underscores=False,
                    escape_asterisks=False, escape_misc=False).convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md, refs


BLOCK_TEXT_KEYS = ("title", "heading", "text", "content", "html", "body", "description", "caption", "label",
                   "question", "answer")


def block_editor_html(attrs: Any) -> str:
    """HTML for a page made with Canvas's block editor, whose `body` can be empty.

    The page's `block_editor_attributes.blocks` holds the layout as JSON (a tree of blocks, each with props
    such as text, title, src and href). Every block's text, pictures and links are kept, in page order."""
    blocks = attrs.get("blocks") if isinstance(attrs, dict) else None
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            return ""
    out: List[str] = []
    if isinstance(blocks, dict) and isinstance(blocks.get("ROOT"), dict):
        seen: set = set()

        def visit(node_id: str) -> None:
            node = blocks.get(node_id)
            if node_id in seen or not isinstance(node, dict) or node.get("hidden"):
                return
            seen.add(node_id)
            kind = node.get("type")
            name = (kind.get("resolvedName") if isinstance(kind, dict) else str(kind or "")) or node.get("displayName") or ""
            _block_html(str(name), node.get("props") or {}, out)
            for child in node.get("nodes") or []:
                visit(child)
            for child in (node.get("linkedNodes") or {}).values():
                visit(child)

        visit("ROOT")
    elif isinstance(blocks, (dict, list)):
        _walk_blocks(blocks, out, 0)
    return "\n".join(out)


def _walk_blocks(value: Any, out: List[str], depth: int) -> None:
    if depth > 40:
        return
    if isinstance(value, list):
        for item in value:
            _walk_blocks(item, out, depth + 1)
    elif isinstance(value, dict):
        props = value.get("props") if isinstance(value.get("props"), dict) else value
        _block_html(str(value.get("type") or value.get("name") or ""), props, out)
        for key, item in value.items():
            if isinstance(item, (dict, list)) and key != "props":
                _walk_blocks(item, out, depth + 1)


def _block_html(name: str, props: Dict[str, Any], out: List[str]) -> None:
    if not isinstance(props, dict):
        return
    low = name.lower()
    href = props.get("href") or props.get("linkUrl") or props.get("link")
    href = href if isinstance(href, str) and href.strip() else ""
    src = props.get("src") or props.get("url") or props.get("imageUrl")
    src = src if isinstance(src, str) and src.strip() else ""
    for key in BLOCK_TEXT_KEYS:
        value = props.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if href and key in ("text", "label", "title"):
            out.append(f'<p><a href="{escape(href, quote=True)}">{escape(value.strip())}</a></p>')
            href = ""
        elif "<" in value and ">" in value:
            out.append(value)
        elif "heading" in low or key in ("title", "heading"):
            level = props.get("level")
            level = int(re.sub(r"\D", "", str(level)) or 2) if level else 2
            level = min(max(level, 1), 6)
            out.append(f"<h{level}>{escape(value.strip())}</h{level}>")
        else:
            out.append(f"<p>{escape(value.strip())}</p>")
    if href:
        out.append(f'<p><a href="{escape(href, quote=True)}">{escape(href)}</a></p>')
    if src:
        alt = props.get("alt") or props.get("altText") or ""
        if any(w in low for w in ("video", "media", "embed", "iframe")) or YOUTUBE_RE.search(src):
            out.append(f'<iframe src="{escape(src, quote=True)}" title="{escape(str(alt), quote=True)}"></iframe>')
        elif "image" in low or re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", src, re.I) or "/files/" in src:
            out.append(f'<img src="{escape(src, quote=True)}" alt="{escape(str(alt), quote=True)}">')
        else:
            out.append(f'<p><a href="{escape(src, quote=True)}">{escape(str(alt) or src)}</a></p>')


TOKEN_RE = re.compile(r"\((canvas-(file|page|assignment|quiz|discussion|media|youtube):([^)\s]+))(?:\s+\"[^\"]*\")?\)")


def simple_html_to_markdown(html: Optional[str]) -> str:
    """HTML from a local file (e.g. a converted Word doc): convert without touching links."""
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    _prepare(soup)
    md = _Converter(heading_style="ATX", bullets="-", escape_underscores=False,
                    escape_asterisks=False, escape_misc=False).convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def html_text(html: Optional[str]) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
