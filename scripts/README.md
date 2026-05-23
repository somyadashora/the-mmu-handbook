# MMU Handbook — PDF Build Scripts

Converts all 18 chapters of the MMU Handbook into a single print-ready A4 PDF
using **cairosvg** (SVG → PDF conversion) and **pandoc/extra** (Markdown → PDF
via XeLaTeX + the Eisvogel template).

---

## Quick start

```bash
# One-time: install Python dependencies
pip3 install cairosvg beautifulsoup4

# Full build (chapters 1–18)
./scripts/build_pdf.sh

# Output:
#   output/mmu-handbook.pdf
```

The `pandoc/extra:latest` Docker image is required and is already present in
this dev environment. If you need to pull it manually:

```bash
docker pull pandoc/extra:latest
```

---

## Scripts

| File | Purpose |
|---|---|
| `build_pdf.sh` | Main driver — runs preprocessing then the Docker compile step |
| `preprocess.py` | Strips inner-document TOCs, extracts inline SVGs → PDF figures, writes `build/book.md` |
| `metadata.yaml` | Pandoc/Eisvogel book metadata (title page, page size, fonts, colours) |

---

## Pipeline detail

```
chapters-md/chapter-01…16.md  ──┐
                                 ├─► preprocess.py ──► build/book.md ──► pandoc/extra ──► output/mmu-handbook.pdf
chapters/chapter-17…18.html   ──┘        │
                                          └──► build/figures/*.pdf  (inline SVGs)
```

### Stage 1 — `preprocess.py`

Runs on the **host** (requires `cairosvg`, `beautifulsoup4`, `pandoc`).

- **Chapters 1–16** (Markdown source in `chapters-md/`):
  - Removes the pandoc-generated inner-document TOC bullet list
  - Removes the `:::` title-block-header div
  - Removes `# Part X:` headings (pandoc generates book parts from the
    `documentclass=book` class; the inline Part headers would be redundant)
  - Replaces each `<figure><svg>…</svg><figcaption>…</figcaption></figure>`
    block with `![caption](../figures/chXX-figNNNN.pdf){width=95%}`

- **Chapters 17–18** (HTML source in `chapters/`, no Markdown):
  - Parses HTML with BeautifulSoup
  - Strips the sidebar `<script>`, `<style>`, `<link>`, and `<nav id="TOC">`
  - Converts SVG figures to PDF (same process as above)
  - Converts the cleaned HTML body → Markdown via `pandoc -f html -t markdown`

- Combines all processed chapter files into `build/book.md`, separated by
  `\newpage` raw-LaTeX fences.

### Stage 2 — `pandoc/extra` Docker

Runs in the **`pandoc/extra:latest`** container (requires Docker).

```
pandoc book.md \
  --metadata-file=metadata.yaml \
  --pdf-engine=xelatex \
  --template=eisvogel \
  --top-level-division=chapter \
  --toc --toc-depth=2
```

The Eisvogel template is bundled in `pandoc/extra`. It produces:
- A full-colour title page (navy blue matching the website)
- Alternating left/right running headers (book mode)
- A linked table of contents
- A4 paper with 2.5–2.8 cm margins (comfortable for printing and binding)

---

## Partial builds

```bash
# Only preprocess chapters 17–18 (useful when adding new chapters)
python3 scripts/preprocess.py --chapters 17-18

# Recompile PDF without re-extracting figures (fast iteration on metadata.yaml)
./scripts/build_pdf.sh --quick

# Preprocess and compile a specific range
./scripts/build_pdf.sh --chapters 11-14
```

---

## Customising the output

Edit **`scripts/metadata.yaml`** to change:

| Variable | Effect |
|---|---|
| `title` / `subtitle` | Title page text |
| `titlepage-color` | Background hex colour of title page |
| `fontsize` | Body font size (`10pt`, `11pt`, `12pt`) |
| `geometry` | Page margins |
| `toc-depth` | TOC depth (`1` = chapters only, `2` = + sections) |
| `colorlinks` | Coloured hyperlinks in the PDF |

---

## Generated files (gitignored)

```
scripts/build/
  figures/          # Extracted SVG → PDF files  (chXX-figNNNN.pdf)
  chapters/         # Per-chapter processed Markdown
  book.md           # Combined book source for pandoc

output/
  mmu-handbook.pdf  # Final print-ready PDF
```

These directories are regenerated on every full build and are listed in
`.gitignore`.
