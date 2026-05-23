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
# Matches the pandoc ::: title-block-header div
TITLE_BLOCK_RE = re.compile(
    r'^:{3,}\s*\{#title-block-header\}.*?:{3,}\s*\n?',
    re.DOTALL | re.MULTILINE,
)
# Matches a Part header at the H1 level (# Part I: …)
PART_HDR_RE = re.compile(r'^# Part [IVX]+:.*\n?', re.MULTILINE)

# ── Emoji → LaTeX symbol map ───────────────────────────────────────────────────
# XeLaTeX's default font (Source Sans 3) lacks most emoji code points.
# We replace common emoji with LaTeX-safe equivalents before compilation.
_EMOJI_MAP: list[tuple[str, str]] = [
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
    # Arrows (often used in tables)
    ('→', r'$\rightarrow$'),         # →
    ('←', r'$\leftarrow$'),          # ←
    ('↔', r'$\leftrightarrow$'),     # ↔
    ('⇒', r'$\Rightarrow$'),         # ⇒
    # Bullets / symbols
    ('•', r'\textbullet{}'),         # •
    ('▶', r'$\blacktriangleright$'), # ▶
]

def _replace_emoji(text: str) -> str:
    """Replace emoji and unsupported Unicode with LaTeX-safe equivalents."""
    for char, replacement in _EMOJI_MAP:
        text = text.replace(char, replacement)
    return text


# ── Global figure counter (unique across all chapters) ─────────────────────────
_fig_counter = 0


def _next_fig_name(ch_num: int) -> str:
    global _fig_counter
    _fig_counter += 1
    return f"ch{ch_num:02d}-fig{_fig_counter:04d}"


def _strip_tags(html_text: str) -> str:
    return TAG_RE.sub('', html_text).strip()


def _clean_caption(raw: str) -> str:
    """Strip HTML tags and normalise whitespace in a caption string."""
    return ' '.join(_strip_tags(raw).split())


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
    text = _replace_emoji(text)

    out = CHAPTERS_OUT / f"chapter-{ch_num:02d}.md"
    out.write_text(text, encoding='utf-8')
    return out


# ── HTML chapter processing (chapters without a Markdown source) ──────────────

def process_html_chapter(ch_num: int) -> pathlib.Path:
    """
    Extract and process a chapter from its HTML source.
    Used for chapters 17–21 which have no Markdown source.
    """
    src  = HTML_SRC / f"chapter-{ch_num:02d}-WITH-FIGURES.html"
    soup = BeautifulSoup(src.read_text(encoding='utf-8'), 'html.parser')

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
    for fig in list(body.find_all('figure')):
        svg_tag = fig.find('svg')
        if not svg_tag:
            fig.decompose()
            continue

        figcap   = fig.find('figcaption')
        caption  = figcap.get_text(strip=True) if figcap else ''
        caption  = ' '.join(caption.split())

        name     = _next_fig_name(ch_num)
        pdf_path = FIGURES_DIR / f"{name}.pdf"

        if not svg_to_pdf(str(svg_tag), pdf_path):
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
    md_text = _replace_emoji(md_text)

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

        if md_src.exists():
            print(f"  [md  ] Chapter {ch_num:02d} … ", end='', flush=True)
            out = process_md_chapter(ch_num)
            size_kb = out.stat().st_size // 1024
            print(f"✓  {out.name}  ({size_kb} KB)")
            chapter_files.append(out)

        elif html_src.exists():
            print(f"  [html] Chapter {ch_num:02d} … ", end='', flush=True)
            out = process_html_chapter(ch_num)
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
