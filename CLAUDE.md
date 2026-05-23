# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A static-HTML technical book: **Memory Management Units and TLBs: Architecture, Implementation, and AI Workloads**. 18 chapters, each a self-contained HTML file. Hosted on GitHub Pages at `kalairajah-personal.github.io/the-mmu-handbook`.

No build server, no package manager. The repo is pure HTML + inline SVG + vanilla JS.

## File Layout

| Path | Purpose |
|---|---|
| `index.html` | GitHub Pages landing page with sidebar nav |
| `chapters/chapter-XX-WITH-FIGURES.html` | Published chapter files (18 chapters) |
| `chapters-md/chapter-XX-WITH-FIGURES.md` | Markdown source for chapters 1–16 |
| `.githooks/pre-commit` | Quality gate (runs audit before committing chapters) |
| `.githooks/install.sh` | Installs the pre-commit hook |

Chapters 17 and 18 exist only as HTML (no corresponding `.md` source).

## Converting Markdown to HTML

Chapters are authored in `chapters-md/` and converted to `chapters/` via **Pandoc**:

```sh
pandoc chapters-md/chapter-XX-WITH-FIGURES.md \
  --standalone --to html5 \
  -o chapters/chapter-XX-WITH-FIGURES.html
```

After conversion, the sidebar `<script>` block must be appended manually to the generated HTML (see Sidebar Architecture below).

## Pre-commit Quality Gate

Install the hook once after cloning:

```sh
sh .githooks/install.sh
```

The gate fires only when `chapters/chapter-*.html` files are staged. It runs:

```sh
python3 /mnt/skills/user/chapter-structure/scripts/audit.py chapters/
```

To run it manually at any time:

```sh
python3 /mnt/skills/user/chapter-structure/scripts/audit.py chapters/
```

To bypass in emergencies (non-chapter commits only):

```sh
git commit --no-verify
```

## Sidebar Architecture

Every HTML file embeds an identical sidebar via a `<script>` block at the end of `<body>`. The script builds the sidebar DOM from a hardcoded `CHAPTERS` array (JSON) and injects it into the page.

**Two sidebar variants exist:**

1. **`index.html`** — landing page version. Renders chapter titles as plain links (no TOC expand/collapse). The `CHAPTERS` array references `chapters/chapter-XX-...` paths.

2. **Each `chapters/*.html`** — full chapter version. Detects the current chapter by filename, marks it active, expands its TOC, and supports section-highlighting as you scroll. References `../index.html` for the Home link and sibling chapters via relative paths (`chapter-XX-...`).

### When adding a new chapter

You must update the `CHAPTERS` array in **two places**:

1. `index.html` — add an entry with `"num"`, `"file"`, `"short"`, and `"sections"`.
2. **Every existing chapter HTML file** (`chapters/chapter-*.html`) — add the same entry to their embedded `CHAPTERS` array so the sidebar stays in sync across all pages.

The array is found near the bottom of each file inside the `<script>(function() { var CHAPTERS = [` block.

### Section IDs

Sidebar TOC entries reference Pandoc-generated `id` attributes on headings. When editing or adding sections in Markdown, verify that the `id` values in the `CHAPTERS` array match the actual heading IDs Pandoc produces (`<h2 id="section-X.Y">`).

## Viewing the Site Locally

Open any file directly in a browser — no server required:

```sh
open index.html
# or a specific chapter:
open chapters/chapter-01-WITH-FIGURES.html
```

The sidebar collapse state persists via `localStorage`. Toggle with the `\` key.

## Inline SVG Figures

All diagrams are embedded inline SVG — there are no external image files. When editing figures, modify the `<svg>` blocks directly inside the HTML. Chapter 1 has 23 figures; the audit script enforces minimum figure counts per chapter.

## Citation Standards

Every chapter must include IEEE-style references. Minimum: 8 per chapter; AI/ML chapters (11–14) require ≥ 12. The audit script checks this.

---

## Generating the Print PDF

The `scripts/` directory contains a full pipeline that compiles all 18 chapters into a single A4 PDF with title page, table of contents, and all SVG figures rendered inline.

### One-command build

```sh
# Install Python deps once
pip3 install cairosvg beautifulsoup4

# Full build (both stages)
./scripts/build_pdf.sh

# Output: output/mmu-handbook.pdf  (~1,260 pages, ~3.3 MB)
```

Requires Docker with `pandoc/extra:latest` (already present).

### Pipeline stages

| Stage | Tool | What it does |
|---|---|---|
| 1 — preprocess | `scripts/preprocess.py` (Python, host) | Strips inner TOCs, extracts SVG → PDF via cairosvg, writes `scripts/build/book.md` |
| 2 — compile | `pandoc/extra` Docker + XeLaTeX | Converts `book.md` → A4 PDF via Eisvogel template |

### Partial / fast builds

```sh
# Skip preprocessing (reuse existing build/)
./scripts/build_pdf.sh --quick

# Only process specific chapters
./scripts/build_pdf.sh --chapters 17-18

# Run preprocessor alone
python3 scripts/preprocess.py --chapters 1-18
```

### Customising the PDF

Edit `scripts/metadata.yaml` to change the title page, page margins, font size, TOC depth, or link colours. The key Eisvogel variables are documented in comments in that file.

### Known SVG quirks handled by the preprocessor

Two SVG authoring issues are silently fixed during preprocessing:

1. **`height:auto` without `viewBox`** — Some SVGs specify `height:auto` in the `style` attribute without a `viewBox`, causing cairosvg to report "SVG size is undefined". The preprocessor adds `viewBox="0 0 W H"` to give it a size reference.

2. **Duplicate XML attributes** — Some SVGs have the same attribute name twice on an element (e.g. `font-family` appearing twice on a `<text>` element). The preprocessor deduplicates these, keeping the last occurrence.
