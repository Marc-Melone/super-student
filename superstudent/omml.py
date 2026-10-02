"""Office equations (OMML, used by PowerPoint's and Word's equation editors) as plain linear text.

Equation boxes would otherwise vanish from the text versions: python-pptx skips the shapes that contain
them and Word-to-HTML conversion drops them. Linear form keeps every symbol and the structure:
fractions become (a)/(b), powers ^(...), subscripts _(...), sums ∑_(t=1)^(T) ..., roots √(...).
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ATOM = re.compile(r"[\w.′'*∞]+|.")


def _name(el) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.rsplit("}", 1)[-1]


def _enclosed(text: str) -> bool:
    if len(text) < 2 or text[0] not in "([{" or text[-1] != {"(": ")", "[": "]", "{": "}"}[text[0]]:
        return False
    depth = 0
    for i, ch in enumerate(text):
        depth += ch in "([{"
        depth -= ch in ")]}"
        if depth == 0 and i < len(text) - 1:
            return False
    return True


def _wrap(text: str) -> str:
    text = text.strip()
    return text if ATOM.fullmatch(text) or _enclosed(text) else f"({text})"


def _child(el, name: str):
    return el.find(f"{M}{name}")


def _prop(el, prop: str, attr: str, default: str) -> str:
    pr = el.find(f"{M}{_name(el)}Pr")
    node = pr.find(f"{M}{attr}") if pr is not None else None
    if node is None:
        return default
    return node.get(f"{M}val", default)


def to_linear(el) -> str:
    name = _name(el)
    if name == "t":
        return el.text or ""
    if name in ("rPr", "ctrlPr") or name.endswith("Pr"):
        return ""
    kids = list(el)
    if name == "f":
        num, den = _child(el, "num"), _child(el, "den")
        return f"{_wrap(to_linear(num)) if num is not None else ''}/{_wrap(to_linear(den)) if den is not None else ''}"
    if name in ("sSup", "sSub", "sSubSup", "sPre"):
        base = _child(el, "e")
        sub, sup = _child(el, "sub"), _child(el, "sup")
        b = _wrap(to_linear(base)) if base is not None else ""
        lo = f"_{_wrap(to_linear(sub))}" if sub is not None else ""
        hi = f"^{_wrap(to_linear(sup))}" if sup is not None else ""
        return f"{lo}{hi}{b}" if name == "sPre" else f"{b}{lo}{hi}"
    if name == "nary":
        op = _prop(el, "nary", "chr", "∫")
        sub, sup, body = _child(el, "sub"), _child(el, "sup"), _child(el, "e")
        lo = f"_{_wrap(to_linear(sub))}" if sub is not None and to_linear(sub).strip() else ""
        hi = f"^{_wrap(to_linear(sup))}" if sup is not None and to_linear(sup).strip() else ""
        return f"{op}{lo}{hi} {to_linear(body) if body is not None else ''}".rstrip()
    if name == "rad":
        deg, body = _child(el, "deg"), _child(el, "e")
        inner = to_linear(body) if body is not None else ""
        d = to_linear(deg).strip() if deg is not None else ""
        return f"root{_wrap(d)}({inner})" if d else f"√({inner})"
    if name == "d":
        beg = _prop(el, "d", "begChr", "(")
        end = _prop(el, "d", "endChr", ")")
        sep = _prop(el, "d", "sepChr", "|")
        parts = [to_linear(e) for e in el.findall(f"{M}e")]
        return f"{beg}{sep.join(parts)}{end}"
    if name == "func":
        fname, body = _child(el, "fName"), _child(el, "e")
        return f"{to_linear(fname) if fname is not None else ''}{_wrap(to_linear(body)) if body is not None else ''}"
    if name in ("limLow", "limUpp"):
        body, lim = _child(el, "e"), _child(el, "lim")
        mark = "_" if name == "limLow" else "^"
        return f"{to_linear(body) if body is not None else ''}{mark}{_wrap(to_linear(lim)) if lim is not None else ''}"
    if name in ("acc", "groupChr"):
        mark = _prop(el, name, "chr", "̂" if name == "acc" else "⏟")
        body = _child(el, "e")
        return f"{_wrap(to_linear(body)) if body is not None else ''}{mark}"
    if name == "bar":
        body = _child(el, "e")
        return f"bar{_wrap(to_linear(body)) if body is not None else ''}"
    if name == "m":
        rows = []
        for mr in el.findall(f"{M}mr"):
            rows.append(", ".join(to_linear(e) for e in mr.findall(f"{M}e")))
        return "[" + "; ".join(rows) + "]"
    if name == "eqArr":
        return "; ".join(to_linear(e) for e in el.findall(f"{M}e"))
    return "".join(to_linear(k) for k in kids)


def math_text(el) -> str:
    """Linear text of an m:oMathPara or m:oMath element (or anything containing them)."""
    maths = [el] if _name(el) in ("oMath",) else el.iter(f"{M}oMath")
    out = [re.sub(r"\s+", " ", to_linear(m)).strip() for m in maths]
    return "; ".join(x for x in out if x)


def docx_with_linear_math(path: Path) -> Optional[Path]:
    """A temporary copy of a Word file with every equation replaced by its linear text in brackets, so the
    usual Word conversion keeps it in place. None when the file has no equations (or can't be rewritten)."""
    from lxml import etree

    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
            if b"<m:oMath" not in xml:
                return None
            root = etree.fromstring(xml)
            changed = 0
            for outer in list(root.iter(f"{M}oMathPara")) + [m for m in root.iter(f"{M}oMath")
                                                              if m.getparent() is not None and _name(m.getparent()) != "oMathPara"]:
                parent = outer.getparent()
                if parent is None:
                    continue
                text = math_text(outer)
                run = etree.Element(f"{W}r")
                t = etree.SubElement(run, f"{W}t")
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                t.text = f" [equation: {text}] " if text else " [equation] "
                parent.replace(outer, run)
                changed += 1
            if not changed:
                return None
            tmp = Path(tempfile.mkdtemp(prefix="ss-math-")) / path.name
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
                for item in z.infolist():
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True) \
                        if item.filename == "word/document.xml" else z.read(item.filename)
                    out.writestr(item, data)
            return tmp
    except Exception:
        return None


def cleanup(tmp: Optional[Path]) -> None:
    if tmp is not None:
        shutil.rmtree(tmp.parent, ignore_errors=True)
