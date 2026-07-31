"""Exact-copy checks for HTML/SoT labels — rendered UI text, not bare substrings.

Compliance used to pass when a required string appeared *anywhere* in a touched
file (comments, dead constants, docs). These helpers ask a narrower question:
does the string appear as user-visible UI copy in source?
"""

from __future__ import annotations

import re

# Block comments then line comments (JS/TS/Python/#). Strings may still contain
# comment-like sequences; this is best-effort, not a full parser.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(
    r"^\s*//.*?$|^\s*#.*?$|(?<=\S)\s+//.*?$",
    re.M,
)


def strip_code_comments(source: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", source)
    return _LINE_COMMENT_RE.sub(" ", text)


def appears_as_rendered_ui_text(source: str, needle: str) -> bool:
    """True when ``needle`` looks like rendered UI copy in ``source``.

    Accepts:
    - JSX/HTML text nodes: ``>My datasets<``
    - Exact string / template literals: ``"My datasets"`` / ``'…'`` / ```…```
    - JSX expression children: ``{"My datasets"}``
    - Visible attrs: aria-label / title / placeholder / alt
    - Common UI props: ``title: "…"``, ``label: "…"``

    Rejects comment-only hits and bare substrings inside longer unrelated text.
    """
    needle = (needle or "").strip()
    if not needle or needle not in source:
        return False
    body = strip_code_comments(source)
    if needle not in body:
        return False

    esc = re.escape(needle)
    patterns = (
        # HTML / JSX text node between tags
        rf">\s*{esc}\s*<",
        # Exact quoted or template literal
        rf"""(["'`]){esc}\1""",
        # JSX {"…"} / {'…'}
        rf"""\{{\s*["']{esc}["']\s*\}}""",
        # Visible attributes
        rf"""(?:aria-label|title|placeholder|alt)\s*=\s*(["']){esc}\1""",
        # UI object / JSX props
        rf"""\b(?:title|label|heading|caption|placeholder|text|buttonText|ariaLabel|name)\s*[:=]\s*(["']){esc}\1""",
    )
    return any(re.search(p, body) for p in patterns)


def missing_rendered_exact_texts(sources: list[str], exact: list[str]) -> list[str]:
    """Return exact strings not present as rendered UI copy in any source blob."""
    missing: list[str] = []
    for text in exact:
        t = str(text).strip()
        if not t:
            continue
        if not any(appears_as_rendered_ui_text(src, t) for src in sources):
            missing.append(t)
    return missing
