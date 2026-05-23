#!/usr/bin/env python3
"""
MMU Handbook — Chapter preprocessor for PDF compilation.

For every chapter this script:
  1. Strips the pandoc-generated inner-document TOC (HTML chapters have a
     <nav id="TOC">; Markdown chapters have a nested bullet list).
  2. Extracts every inline <svg> figure → writes to build/figures/chXX-figNNNN.svg
  3. Converts each SVG → PDF via cairosvg (lossless, vector).
  4. Replaces the original figure block with a pandoc image reference
     ![caption](../figures/chXX-figNNNN.pdf){width=95%}
  5. Writes the cleaned per-chapter Markdown to build/chapters/chapterXX.md.
  6. Concatenates all chapters into build/book.md (pandoc's single input file).

Chapters 1–16 are read from chapters-md/ (Markdown source).
Chapters 17–18 are read from chapters/ (HTML, no Markdown source exists).

Usage:
    python3 scripts/preprocess.py [--chapters 1-18]

Requirements (host):
    pip install cairosvg beautifulsoup4
"""

import argparse
import os
import re
import subprocess
import sys
import pathlib
from typing import Optional

try:
    import cairosvg
except ImportError:
    sys.exit("ERROR: cairosvg is required.  Run: pip3 install cairosvg")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("ERROR: beautifulsoup4 is required.  Run: pip3 install beautifulsoup4")

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO         = pathlib.Path(__file__).resolve().parent.parent
MD_SRC       = REPO / "chapters-md"
HTML_SRC     = REPO / "chapters"
BUILD_DIR    = pathlib.Path(__file__).resolve().parent / "build"
FIGURES_DIR  = BUILD_DIR / "figures"
CHAPTERS_OUT = BUILD_DIR / "chapters"
OUTPUT_DIR   = REPO / "output"

for _d in [BUILD_DIR, FIGURES_DIR, CHAPTERS_OUT, OUTPUT_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Compiled patterns ──────────────────────────────────────────────────────────
# Matches a complete <figure>…</figure> block (may contain SVG)
FIGURE_RE = re.compile(
    r'<figure(?:\s[^>]*)?>.*?</figure>',
    re.DOTALL | re.IGNORECASE,
)
# Matches a complete <svg>…</svg> element
SVG_RE = re.compile(
    r'<svg(?:\s[^>]*)?>.*?</svg>',
    re.DOTALL | re.IGNORECASE,
)
# Matches figcaption content
CAPTION_RE = re.compile(
    r'<figcaption[^>]*>(.*?)</figcaption>',
    re.DOTALL | re.IGNORECASE,
)
# Strips all HTML tags (used to clean caption text)
TAG_RE = re.compile(r'<[^>]+>')
# Strips a leading "Figure 1.1:" / "Fig. 3:" / "Figure 1:" label from captions.
# Pandoc adds its own "Figure N:" counter, so the HTML label creates a double label.
_FIG_LABEL_RE = re.compile(r'^Fig(?:ure)?\.?\s+[\d.]+[.:]?\s*', re.IGNORECASE)

# Matches the pandoc ::: title-block-header div
TITLE_BLOCK_RE = re.compile(
    r'^:{3,}\s*\{#title-block-header\}.*?:{3,}\s*\n?',
    re.DOTALL | re.MULTILINE,
)
# Matches a Part header at the H1 level (# Part I: …)
PART_HDR_RE = re.compile(r'^# Part [IVX]+:.*\n?', re.MULTILINE)

# ── Emoji → LaTeX symbol maps ──────────────────────────────────────────────────
# XeLaTeX's default font (Source Sans 3) lacks most emoji code points.
# Two maps: prose gets LaTeX math; code blocks get ASCII digraphs so that
# lstlisting verbatim environments don't contain unrenderable LaTeX commands.

_EMOJI_PROSE_MAP: list[tuple[str, str]] = [
    # Unicode variation selectors (invisible; just remove them)
    ('︎', ''),
    ('️', ''),
    # Checkmarks and crosses
    ('✅', r'$\checkmark$'),          # ✅  white heavy check mark
    ('❌', r'$\times$'),              # ❌  cross mark
    ('✔', r'$\checkmark$'),          # ✔  heavy check mark
    ('✖', r'$\times$'),              # ✖  heavy multiplication x
    # Warning / info
    ('⚠', '[!]'),                    # ⚠  warning sign
    ('ℹ', '[i]'),                    # ℹ  information source
    # Arrows (often used in prose and tables)
    ('→', r'$\rightarrow$'),         # →
    ('←', r'$\leftarrow$'),          # ←
    ('↔', r'$\leftrightarrow$'),     # ↔
    ('⇒', r'$\Rightarrow$'),         # ⇒
    ('↓', r'$\downarrow$'),          # ↓
    ('↑', r'$\uparrow$'),            # ↑
    # Checkmark variants
    ('✓', r'$\checkmark$'),          # ✓  light check mark (U+2713)
    # Bullets / symbols
    ('•', r'\textbullet{}'),         # •
    ('▶', r'$\blacktriangleright$'), # ▶
]

_EMOJI_CODE_MAP: list[tuple[str, str]] = [
    # Unicode variation selectors (invisible; just remove them)
    ('︎', ''),
    ('️', ''),
    # Checkmarks and crosses → ASCII
    ('✅', '[OK]'), ('❌', '[X]'), ('✔', '[OK]'), ('✖', '[X]'),
    # Warning / info
    ('⚠', '[!]'), ('ℹ', '[i]'),
    # Arrows → ASCII digraphs
    ('→', '->'), ('←', '<-'), ('↔', '<->'), ('⇒', '=>'),
    ('↓', '|'), ('↑', '^'),
    # Checkmark variants
    ('✓', '[OK]'),
    # Bullets / symbols
    ('•', '*'), ('▶', '>'),
]


def _replace_emoji_smart(md: str) -> str:
    """
    Replace emoji/symbols context-aware:
      - Inside fenced code blocks (```...```) → ASCII digraphs (_EMOJI_CODE_MAP)
      - Outside code blocks (prose, tables, headings) → LaTeX math (_EMOJI_PROSE_MAP)
    """
    parts: list[str] = []
    _FENCE_LINE = re.compile(r'^(`{3,})[^\n]*', re.MULTILINE)
    pos = 0
    in_fence = False
    fence_ticks = 0

    for m in _FENCE_LINE.finditer(md):
        ticks = len(m.group(1))
        if not in_fence:
            # Prose chunk before this opening fence
            chunk = md[pos : m.start()]
            for char, repl in _EMOJI_PROSE_MAP:
                chunk = chunk.replace(char, repl)
            parts.append(chunk)
            parts.append(m.group(0))
            pos = m.end()
            in_fence = True
            fence_ticks = ticks
        else:
            # Closing fence (must have at least as many ticks as the opener)
            if ticks >= fence_ticks:
                # Code chunk inside the fence
                chunk = md[pos : m.start()]
                for char, repl in _EMOJI_CODE_MAP:
                    chunk = chunk.replace(char, repl)
                parts.append(chunk)
                parts.append(m.group(0))
                pos = m.end()
                in_fence = False
                fence_ticks = 0

    # Remaining text after the last fence line
    chunk = md[pos:]
    if in_fence:
        for char, repl in _EMOJI_CODE_MAP:
            chunk = chunk.replace(char, repl)
    else:
        for char, repl in _EMOJI_PROSE_MAP:
            chunk = chunk.replace(char, repl)
    parts.append(chunk)
    return ''.join(parts)


# ── Global figure counter (unique across all chapters) ─────────────────────────
_fig_counter = 0


def _next_fig_name(ch_num: int) -> str:
    global _fig_counter
    _fig_counter += 1
    return f"ch{ch_num:02d}-fig{_fig_counter:04d}"


def _strip_tags(html_text: str) -> str:
    return TAG_RE.sub('', html_text).strip()


def _clean_caption(raw: str) -> str:
    """Strip HTML tags, normalise whitespace, and remove leading figure-label prefix."""
    text = ' '.join(_strip_tags(raw).split())
    return _FIG_LABEL_RE.sub('', text)


# ── SVG → PDF conversion ───────────────────────────────────────────────────────

def _sanitise_svg(svg: str) -> str:
    """
    Pre-process an SVG string to fix known cairosvg compatibility issues.

    Fix 1 — missing ``viewBox`` (root ``<svg>`` only):
        When an SVG has explicit ``width``/``height`` attributes but no
        ``viewBox``, and the CSS ``style`` contains ``height:auto``, cairosvg
        cannot determine the canvas size and raises "SVG size is undefined".
        Adding ``viewBox="0 0 W H"`` gives cairosvg a reliable size reference
        without altering the rendered appearance.

    Fix 2 — duplicate XML attributes (all elements):
        Duplicate attribute names (e.g. two ``font-family=`` on the same
        ``<text>`` element) cause a parse error in cairosvg.  We deduplicate
        by scanning every opening tag and keeping only the *last* occurrence
        of each attribute name, consistent with browser behaviour.

    Fix 3 — undefined marker references:
        If a path/line/polyline uses ``marker-end="url(#some-id)"`` but no
        ``<marker id="some-id">`` is defined in the SVG, cairosvg crashes with
        ``AttributeError: 'NoneType' object has no attribute 'get'``.  Browsers
        handle this gracefully by simply not drawing the missing marker.  We
        strip any marker-start/mid/end references whose target ID is not defined
        within the SVG.

    Fix 4 — unescaped XML special characters in text nodes:
        SVGs authored for HTML may contain bare ``&`` (e.g. "TLB & PWC") or
        ``<`` (e.g. "Miss rate: <0.1%") inside ``<text>`` / ``<tspan>``
        elements.  These are tolerated by browsers but cause cairosvg's XML
        parser to raise "not well-formed (invalid token)".  We locate the
        *trailing* text node in each ``<text>``/``<tspan>`` element (the text
        between the last ``>`` of any child element and the closing tag) and
        escape ``&`` → ``&amp;`` and invalid ``<`` → ``&lt;``.
    """
    # ── Fix 1: add viewBox to root <svg> if missing ────────────────────────────
    def _ensure_viewbox(m: re.Match) -> str:
        tag = m.group(0)
        if re.search(r'\bviewBox\s*=', tag, re.IGNORECASE):
            return tag   # already has a viewBox — nothing to do
        w_m = re.search(r'\bwidth\s*=\s*"(\d+(?:\.\d+)?)"', tag, re.IGNORECASE)
        h_m = re.search(r'\bheight\s*=\s*"(\d+(?:\.\d+)?)"', tag, re.IGNORECASE)
        if w_m and h_m:
            vb = f' viewBox="0 0 {w_m.group(1)} {h_m.group(1)}"'
            # Insert before the closing '>' of the opening tag
            tag = tag[:-1] + vb + '>'
        return tag

    svg = re.sub(
        r'<svg(?:\s[^>]*)?>',
        _ensure_viewbox,
        svg,
        count=1,
        flags=re.IGNORECASE,
    )

    # ── Fix 2: deduplicate attributes on ALL elements ──────────────────────────
    def _dedup_tag_attrs(m: re.Match) -> str:
        tag_name = m.group(1)   # element name, e.g. "text", "rect", "marker"
        inner    = m.group(2)   # full attribute string
        seen: dict[str, str] = {}
        for am in re.finditer(r'''([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''', inner):
            lower_name = am.group(1).lower()
            raw_val    = am.group(2) if am.group(2) is not None else am.group(3)
            # Store with original-case attribute name; last occurrence wins
            seen[lower_name] = f'{am.group(1)}="{raw_val}"'
        rebuilt = (' ' + ' '.join(seen.values())) if seen else ''
        return f'<{tag_name}{rebuilt}>'

    svg = re.sub(
        r'<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'))+)\s*>',
        _dedup_tag_attrs,
        svg,
        flags=re.IGNORECASE,
    )

    # ── Fix 4: escape bare & and < in <text>/<tspan> text nodes ──────────────
    def _fix_text_node(m: re.Match) -> str:
        full = m.group(0)
        tag  = m.group('tag')
        close_tag = f'</{tag}>'
        close_pos = full.rfind(close_tag)
        if close_pos < 0:
            return full
        # The final text node is between the last '>' before the closing tag
        # and the closing tag itself — everything after the last child element.
        last_gt = full.rfind('>', 0, close_pos)
        if last_gt < 0:
            return full
        text = full[last_gt + 1 : close_pos]
        if not text:
            return full
        # Escape bare & (not already an entity reference &amp; &#123; etc.)
        text = re.sub(
            r'&(?![a-zA-Z][a-zA-Z0-9]*;|#[0-9]+;|#x[0-9a-fA-F]+;)',
            '&amp;', text,
        )
        # Escape < not followed by a valid XML name-start char (so real tags survive)
        text = re.sub(r'<(?![a-zA-Z_:/!?])', '&lt;', text)
        return full[: last_gt + 1] + text + full[close_pos:]

    svg = re.sub(
        r'<(?P<tag>text|tspan)\b[^>]*>[\s\S]*?</(?P=tag)>',
        _fix_text_node,
        svg,
        flags=re.IGNORECASE,
    )

    # ── Fix 3: strip marker-* references to undefined marker IDs ──────────────
    # Collect all marker IDs defined in this SVG
    defined_marker_ids: set[str] = set(
        re.findall(r'<marker\b[^>]*\bid=["\']([^"\']+)["\']', svg, re.IGNORECASE)
    )
    if defined_marker_ids:
        # Only strip markers if the SVG actually defines any; otherwise leave as-is
        # (no markers defined + no references = nothing to do)
        def _strip_undef_marker(m: re.Match) -> str:
            url_id = re.search(r'url\(#([^)]+)\)', m.group(0))
            if url_id and url_id.group(1) not in defined_marker_ids:
                return ''   # remove the whole attribute
            return m.group(0)

        svg = re.sub(
            r'\s+marker-(?:start|mid|end)=["\'][^"\']*["\']',
            _strip_undef_marker,
            svg,
            flags=re.IGNORECASE,
        )
    else:
        # No markers defined at all — strip ALL marker-* attributes to be safe
        svg = re.sub(
            r'\s+marker-(?:start|mid|end)=["\'][^"\']*["\']',
            '',
            svg,
            flags=re.IGNORECASE,
        )

    return svg


def svg_to_pdf(svg_content: str, pdf_path: pathlib.Path) -> bool:
    """
    Convert an SVG string to a PDF file at *pdf_path* via cairosvg.
    Applies sanitisation before conversion.
    Returns True on success.  Prints a warning on failure (does not raise).
    """
    clean_svg = _sanitise_svg(svg_content)
    try:
        cairosvg.svg2pdf(
            bytestring=clean_svg.encode('utf-8'),
            write_to=str(pdf_path),
        )
        return True
    except Exception as exc:
        print(f"    ⚠  SVG→PDF failed [{pdf_path.name}]: {exc}", file=sys.stderr)
        return False


# ── Figure block replacement helpers ──────────────────────────────────────────

def _replace_figure_block(block: str, ch_num: int) -> str:
    """
    Given a raw <figure>…</figure> string, extract the SVG, convert to PDF,
    and return a pandoc-compatible figure markdown line.
    Returns '' if no SVG is found or conversion fails.
    """
    svg_match = SVG_RE.search(block)
    if not svg_match:
        return ''

    svg_content  = svg_match.group(0)
    caption_match = CAPTION_RE.search(block)
    caption = _clean_caption(caption_match.group(1)) if caption_match else ''

    name     = _next_fig_name(ch_num)
    pdf_path = FIGURES_DIR / f"{name}.pdf"

    if not svg_to_pdf(svg_content, pdf_path):
        return ''

    # Path relative to BUILD_DIR — pandoc resolves images relative to book.md's location.
    rel = os.path.relpath(pdf_path, BUILD_DIR)
    return f'\n\n![{caption}]({rel}){{width=95%}}\n\n'


# ── Markdown chapter processing (chapters 1–16) ────────────────────────────────

def _extract_chapter_title(text: str) -> str:
    """
    Extract the chapter H1 title from the pandoc title-block-header div.
    Returns the title string (without the div wrapper and id attributes),
    or '' if no title-block-header div is found.
    """
    m = TITLE_BLOCK_RE.search(text)
    if not m:
        return ''
    # The div body is m.group(0); extract the H1 text inside it
    inner = m.group(0)
    h1 = re.search(r'^# (.+?)(?:\s*\{[^}]*\})?\s*$', inner, re.MULTILINE)
    if h1:
        return h1.group(1).strip()
    return ''


def _strip_md_toc(text: str) -> str:
    """
    Remove the pandoc-generated inner-document TOC from a markdown source file.

    Strategy:
      1. Extract the chapter title from the :::title-block-header div before stripping.
      2. Strip the entire ::: div (which contains the H1 title).
      3. Strip the nested bullet-list TOC (everything before the next real heading).
      4. Strip # Part I: … headers (redundant with book documentclass).
      5. Re-prepend the chapter H1 at the top so it becomes the \\chapter{} heading.

    The re-prepend step is critical: without it, chapters whose markdown happens to
    start with a code comment (# comment) or other # line before the real chapter
    heading will be assigned the wrong title (the Chapter 8 / Chapter 13-16 bug).
    """
    # 1. Capture the chapter title before destroying the div
    title = _extract_chapter_title(text)

    # 2. Strip :::…::: title-block-header div
    text = TITLE_BLOCK_RE.sub('', text)

    # 3. Strip the TOC bullet list — find the first # heading that looks like real
    #    content (a Chapter heading or a section heading like ## N.N), discard
    #    everything before it.  We look for '# Chapter' or '## ' to be safe.
    lines = text.split('\n')
    first_content = next(
        (i for i, ln in enumerate(lines)
         if re.match(r'^# Chapter\b|^## \d', ln)),
        None,
    )
    if first_content is not None:
        text = '\n'.join(lines[first_content:])
    else:
        # Fallback: drop everything before the first non-empty, non-list line
        first_h = next(
            (i for i, ln in enumerate(lines)
             if ln.startswith('# ') or ln.startswith('## ')),
            0,
        )
        text = '\n'.join(lines[first_h:])

    # 4. Remove Part-level headings (# Part I: …)
    text = PART_HDR_RE.sub('', text)

    # 5. If we have a captured title AND the text doesn't already open with ANY
    #    chapter-level H1 heading, prepend one.  We check for ANY '# Chapter'
    #    pattern (not an exact match) so we don't double-insert when the content
    #    body already contains a slightly different wording of the title.
    if title and not re.match(r'^# ', text.lstrip()):
        text = f'# {title}\n\n' + text.lstrip()

    return text


def process_md_chapter(ch_num: int) -> pathlib.Path:
    """Process chapters 1–16 from their Markdown source."""
    src  = MD_SRC / f"chapter-{ch_num:02d}-WITH-FIGURES.md"
    text = src.read_text(encoding='utf-8')

    text = _strip_md_toc(text)
    text = FIGURE_RE.sub(lambda m: _replace_figure_block(m.group(0), ch_num), text)
    text = _postprocess_md(text)
    text = _replace_emoji_smart(text)

    out = CHAPTERS_OUT / f"chapter-{ch_num:02d}.md"
    out.write_text(text, encoding='utf-8')
    return out


# ── Markdown post-processing (language tags + pseudo-code detection) ─────────

# Language aliases produced by pandoc's HTML→Markdown converter.
# These must be mapped to names that lstlisting recognises.
_LANG_ALIASES: dict[str, str] = {
    # Bare .sourceCode with no language → unlanguaged
    'sourceCode': '',
    # Map to exact lstlisting built-in names (case-sensitive)
    'c':          'C',
    'cpp':        'C++',
    'c++':        'C++',
    'python':     'Python',
    'py':         'Python',
    'bash':       'bash',
    'sh':         'bash',
    'shell':      'bash',
    'java':       'Java',
    'verilog':    'Verilog',
    # Map to our custom lstdefinelanguage definitions
    'asm':        'assembly',
    'mips':       'assembly',
    # Languages with no good listings equivalent → leave unlanguaged
    'powershell': '',
    'swift':      '',
    'javascript': '',
    'js':         '',
}

# Keywords that strongly suggest pseudo-code / algorithm prose.
_PSEUDO_KW_RE = re.compile(
    r'^\s*(?:'
    r'(?:step|phase|case)\s+\d'        # "Step 1:", "Phase 2:"
    r'|\d+\.\s+[A-Z]'                  # "1. Scan ..."  "3. If ..."
    r'|if\s+\S.*(?:then|:)'            # "if condition then"
    r'|(?:while|for|foreach|for each|repeat)\s'  # loops
    r'|(?:return|output|yield)\s'      # return-like
    r'|function\s+\w+\s*[:(]'         # function header
    r'|procedure\s+\w+'               # procedure header
    r'|algorithm\s+\w+'               # algorithm header
    r')\b',
    re.IGNORECASE | re.MULTILINE,
)

# Patterns that strongly indicate REAL code (not pseudo-code).
_REAL_CODE_RE = re.compile(
    r'(?:'
    r'[^;]{5,};\s*$'              # lines ending in semicolon (C/Java/etc.)
    r'|^\s*[{}]\s*$'             # bare { or } on its own line
    r'|^\s*#\s*(?:include|define|ifdef|ifndef|endif|pragma)'  # C preprocessor
    r'|^\s*(?:import|from\s+\S+\s+import)'   # Python/JS imports
    r'|^\s*(?:def|class|async def)\s+\w+'    # Python definitions
    r'|^\s*[$>#]\s'              # shell prompt ($, #, >)
    r'|^\s*\w+\s*=\s*(?:function|class|new\s+\w+)'  # JS patterns
    r')',
    re.MULTILINE,
)


def _classify_block(content: str) -> str:
    """
    Return 'pseudocode', 'c', or '' (unlanguaged) based on block content.

    Heuristic:
    - If the content matches pseudo-code keywords AND has no real-code patterns
      → 'pseudocode'
    - If content looks like C (pointer ops, struct/typedef keywords) → 'c'
    - Otherwise → '' (unlanguaged, but will still get lstset styling)
    """
    if _REAL_CODE_RE.search(content):
        return ''           # real code: leave unlanguaged (no false highlights)

    if _PSEUDO_KW_RE.search(content):
        return 'pseudocode'

    # ASCII-art diagrams or clock-style step descriptions:
    # e.g. "Example: 8 pages in circular list", "[A=1][A=0]...", "^hand"
    art_lines = sum(
        1 for ln in content.splitlines()
        if re.search(r'\[.*\]|^\s*\^|→|↑|↓|←|Step\s+\d', ln)
    )
    if art_lines >= 2:
        return 'pseudocode'

    return ''


def _postprocess_md(md: str) -> str:
    """
    Post-process the pandoc HTML→Markdown output:

    0.  Convert ``::: {style="...font-family:monospace..."}`` fenced divs to
        proper fenced code blocks so lstlisting renders them with monospace
        styling.  Pandoc HTML→Markdown converts ``<div style="font-family:
        monospace">`` to these `::: div` wrappers; pandoc's LaTeX backend
        silently ignores the wrappers and outputs plain paragraphs.

    0b. Strip remaining ``::: div`` wrapper lines (layout containers, table
        scroll wrappers, info-boxes, etc.) that pandoc's LaTeX backend ignores.
        Requires 3+ colons to avoid matching definition-list markers.

    1.  Normalise ``{.sourceCode .LANG}`` fence tags → clean ``LANG`` names
        that lstlisting recognises (e.g. ``{.sourceCode .c}`` → ``c``).

    2.  Convert 4-space **indented** code blocks to fenced blocks so that
        pandoc's ``--listings`` pipeline routes them through ``lstlisting``
        (which applies our configured background / border styling).
        Blocks that match pseudo-code heuristics are tagged ``pseudocode``;
        others are left unlanguaged but still get the lstset styling.
    """
    # ── 0. Convert font-family:monospace div wrappers to fenced code blocks ────
    def _mono_div_to_fence(m: re.Match) -> str:
        content = m.group(1).strip()
        # Unescape pandoc's backslash-escaped punctuation (e.g. "1\." → "1.",
        # "\[Page Dir\]" → "[Page Dir]", "\<VA\>" → "<VA>")
        content = re.sub(r'\\([^a-zA-Z0-9\n\r])', r'\1', content)
        lang = _classify_block(content)
        fence_open = f'```{lang}' if lang else '```'
        return f'\n{fence_open}\n{content}\n```\n\n'

    md = re.sub(
        r'^:+ \{[^}]*font-family\s*:\s*monospace[^}]*\}\s*\n(.*?)^::+\s*$\n?',
        _mono_div_to_fence,
        md,
        flags=re.MULTILINE | re.DOTALL,
    )

    # ── 0b. Strip remaining ::: div wrapper lines ──────────────────────────────
    # These are layout containers (overflow-x:auto scroll wrappers, ::: container,
    # ::: info-box, etc.) that pandoc's LaTeX backend ignores anyway.
    # Requires 3+ colons to avoid matching definition-list syntax (":  def").
    md = re.sub(r'^:{3,}[^\n]*$\n?', '', md, flags=re.MULTILINE)

    # ── 1. Normalise {.sourceCode .LANG} fenced-block tags ────────────────────
    def _clean_lang_tag(m: re.Match) -> str:
        raw = m.group(1)          # e.g. ".sourceCode .c"
        # Extract language classes (skip .sourceCode itself)
        classes = [c.lstrip('.').lower() for c in raw.split() if c != '.sourceCode']
        lang = _LANG_ALIASES.get(classes[0], classes[0]) if classes else ''
        return f'```{lang}' if lang else '```'

    md = re.sub(
        r'```\s*\{([^}]+)\}',
        _clean_lang_tag,
        md,
    )

    # ── 2. Convert 4-space indented blocks → fenced pseudocode/plain ──────────
    #
    # An indented block in pandoc Markdown is one or more consecutive lines
    # each starting with 4+ spaces (with blank lines allowed inside).
    # We need to be outside a fenced block to process these.
    out_lines: list[str] = []
    in_fence  = False
    fence_pat = re.compile(r'^`{3,}')

    raw_lines = md.split('\n')
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # Track fenced-block state
        if fence_pat.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            i += 1
            continue

        if in_fence:
            out_lines.append(line)
            i += 1
            continue

        # Detect start of a 4-space indented block (outside any fence)
        if re.match(r'^    \S', line):
            # Collect all lines belonging to this indented block
            block_lines: list[str] = []
            j = i
            while j < len(raw_lines):
                ln = raw_lines[j]
                if re.match(r'^    ', ln):            # indented line
                    block_lines.append(ln[4:])         # strip 4-space prefix
                    j += 1
                elif ln.strip() == '':                 # blank line — may be inside block
                    # Look ahead: if next non-blank line is still indented, keep it
                    k = j + 1
                    while k < len(raw_lines) and raw_lines[k].strip() == '':
                        k += 1
                    if k < len(raw_lines) and re.match(r'^    ', raw_lines[k]):
                        block_lines.append('')         # preserve blank line
                        j += 1
                    else:
                        break                          # end of indented block
                else:
                    break

            content = '\n'.join(block_lines)
            lang    = _classify_block(content)
            fence   = f'```{lang}' if lang else '```'

            out_lines.append(fence)
            out_lines.extend(block_lines)
            out_lines.append('```')
            out_lines.append('')          # blank line after the fence

            i = j
            continue

        out_lines.append(line)
        i += 1

    return '\n'.join(out_lines)


# ── HTML chapter processing (primary source for all chapters) ────────────────

def process_html_chapter(ch_num: int) -> pathlib.Path:
    """
    Extract and process a chapter from its deployed HTML source.
    This is the primary pipeline for ALL chapters (1–21): the HTML files in
    chapters/ are the authoritative, deployed versions — identical to the live
    site — and are always preferred over the chapters-md/ Markdown sources,
    which may be draft/incomplete (e.g. ch08 is missing sections 8.1–8.4 and
    ch11 only has one section in its MD file).
    """
    src      = HTML_SRC / f"chapter-{ch_num:02d}-WITH-FIGURES.html"
    raw_html = src.read_text(encoding='utf-8')

    # ── Pre-extract raw SVGs from original HTML BEFORE BeautifulSoup parsing ──
    # html.parser lowercases ALL attribute names (viewBox → viewbox, markerWidth
    # → markerwidth, etc.), which breaks cairosvg.  We extract the raw SVG text
    # from the original file here, then use it for SVG→PDF conversion instead of
    # the BeautifulSoup-mangled version.
    # We only want SVGs that live inside <figure> elements.
    raw_figure_svgs: list[str] = re.findall(
        r'<figure[^>]*>.*?(<svg[\s\S]*?</svg>)[\s\S]*?</figure>',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )

    soup = BeautifulSoup(raw_html, 'html.parser')

    # ── Strip navigation / chrome ──────────────────────────────────────────────
    for el in soup.find_all(['script', 'style', 'link']):
        el.decompose()
    nav = soup.find('nav', id='TOC')
    if nav:
        nav.decompose()

    # ── Capture chapter title before removing the header element ───────────────
    title_h1 = soup.find('h1', class_='title')
    title_text = title_h1.get_text(strip=True) if title_h1 else f"Chapter {ch_num}"
    header_el = soup.find('header', id='title-block-header')
    if header_el:
        header_el.decompose()

    # ── Replace SVG figures with <img> pointing at the converted PDF ───────────
    body = soup.find('body') or soup
    _svg_raw_idx = 0   # index into raw_figure_svgs
    for fig in list(body.find_all('figure')):
        svg_tag = fig.find('svg')
        if not svg_tag:
            fig.decompose()
            continue

        figcap   = fig.find('figcaption')
        caption  = figcap.get_text(strip=True) if figcap else ''
        caption  = _FIG_LABEL_RE.sub('', ' '.join(caption.split()))

        name     = _next_fig_name(ch_num)
        pdf_path = FIGURES_DIR / f"{name}.pdf"

        # Use original-case SVG if available; fall back to BeautifulSoup string
        if _svg_raw_idx < len(raw_figure_svgs):
            svg_src = raw_figure_svgs[_svg_raw_idx]
        else:
            svg_src = str(svg_tag)   # fallback (may have lowercased attrs)
        _svg_raw_idx += 1

        if not svg_to_pdf(svg_src, pdf_path):
            fig.decompose()
            continue

        # Path relative to BUILD_DIR — pandoc resolves images relative to book.md's location.
        rel = os.path.relpath(pdf_path, BUILD_DIR)

        new_fig = soup.new_tag('figure')
        img_tag = soup.new_tag('img', src=rel, alt=caption)
        new_fig.append(img_tag)
        if caption:
            new_cap = soup.new_tag('figcaption')
            new_cap.string = caption
            new_fig.append(new_cap)
        fig.replace_with(new_fig)

    # ── Convert cleaned HTML → Markdown via pandoc ────────────────────────────
    body_html = str(body)
    result = subprocess.run(
        ['pandoc', '-f', 'html', '-t', 'markdown', '--wrap=none'],
        input=body_html.encode('utf-8'),
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"    ⚠  pandoc html→md failed for chapter {ch_num:02d}:\n"
            f"       {result.stderr.decode('utf-8', errors='replace')}",
            file=sys.stderr,
        )

    md_text = result.stdout.decode('utf-8')

    # Prepend the chapter H1 title (pandoc stripped the <header> above)
    md_text = f"# {title_text}\n\n" + md_text.lstrip()
    md_text = _postprocess_md(md_text)       # identify + fence code blocks first
    md_text = _replace_emoji_smart(md_text)  # then apply context-aware symbol replacement

    out = CHAPTERS_OUT / f"chapter-{ch_num:02d}.md"
    out.write_text(md_text, encoding='utf-8')
    return out


# ── Combine chapters into book.md ─────────────────────────────────────────────

def combine_chapters(chapter_files: list[pathlib.Path]) -> pathlib.Path:
    """
    Concatenate all per-chapter markdown files into a single build/book.md.
    Chapters are separated by a raw LaTeX \\newpage so each chapter starts on
    a fresh page even if the preceding chapter ends mid-page.
    (With documentclass=book, \\chapter{} already does this, but the raw
    \\newpage is a cheap safety net for the TOC/front-matter transitions.)
    """
    separator = '\n\n```{=latex}\n\\newpage\n```\n\n'
    parts = [f.read_text(encoding='utf-8') for f in chapter_files]
    book_md = separator.join(parts)

    out = BUILD_DIR / "book.md"
    out.write_text(book_md, encoding='utf-8')
    return out


# ── CLI entry point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Preprocess MMU Handbook chapters for PDF compilation."
    )
    p.add_argument(
        '--chapters', '-c',
        default='1-21',
        metavar='RANGE',
        help='Chapter range to process, e.g. "1-21" or "19-21" (default: 1-21)',
    )
    return p.parse_args()


def parse_chapter_range(spec: str) -> list[int]:
    """Parse '1-18' or '5' or '17-18' into a list of ints."""
    if '-' in spec:
        lo, hi = spec.split('-', 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main() -> None:
    args  = parse_args()
    chaps = parse_chapter_range(args.chapters)

    print("═" * 60)
    print("  MMU Handbook — PDF preprocessor")
    print(f"  Chapters : {chaps[0]}–{chaps[-1]}")
    print(f"  Repo     : {REPO}")
    print(f"  Build    : {BUILD_DIR}")
    print("═" * 60)

    chapter_files: list[pathlib.Path] = []

    for ch_num in chaps:
        md_src   = MD_SRC / f"chapter-{ch_num:02d}-WITH-FIGURES.md"
        html_src = HTML_SRC / f"chapter-{ch_num:02d}-WITH-FIGURES.html"

        # HTML is the authoritative, deployed source — always prefer it.
        # Fall back to MD only when no HTML file exists.
        if html_src.exists():
            print(f"  [html] Chapter {ch_num:02d} … ", end='', flush=True)
            out = process_html_chapter(ch_num)
            size_kb = out.stat().st_size // 1024
            print(f"✓  {out.name}  ({size_kb} KB)")
            chapter_files.append(out)

        elif md_src.exists():
            print(f"  [md  ] Chapter {ch_num:02d} … ", end='', flush=True)
            out = process_md_chapter(ch_num)
            size_kb = out.stat().st_size // 1024
            print(f"✓  {out.name}  ({size_kb} KB)")
            chapter_files.append(out)

        else:
            print(f"  [SKIP] Chapter {ch_num:02d} — no source file found", file=sys.stderr)

    if not chapter_files:
        sys.exit("ERROR: no chapter files processed.")

    print()
    print(f"  Combining {len(chapter_files)} chapters … ", end='', flush=True)
    book_path = combine_chapters(chapter_files)
    size_kb = book_path.stat().st_size // 1024
    print(f"✓  {book_path.name}  ({size_kb} KB)")

    print()
    print(f"  Figures converted : {_fig_counter}")
    print(f"  Combined source   : {book_path}")
    print()
    print("  Next step: run  ./scripts/build_pdf.sh")
    print("═" * 60)


if __name__ == '__main__':
    main()
