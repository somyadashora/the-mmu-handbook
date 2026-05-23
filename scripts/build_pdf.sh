#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  MMU Handbook — PDF build script
#
#  Produces:  output/mmu-handbook.pdf
#
#  Usage:
#    ./scripts/build_pdf.sh              # full build  (chapters 1–18)
#    ./scripts/build_pdf.sh --quick      # skip preprocessing (reuse build/)
#    ./scripts/build_pdf.sh --chapters 17-18   # preprocess + compile subset
#
#  Prerequisites (host):
#    pip3 install cairosvg beautifulsoup4
#    docker pull pandoc/extra            (already present)
#
#  The pandoc/extra Docker image provides:
#    pandoc 3.9, TeX Live 2026 (xelatex/lualatex), eisvogel template,
#    rsvg-convert, pandoc-crossref, and other pandoc filters.
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$REPO/scripts"
BUILD_DIR="$SCRIPTS_DIR/build"
OUTPUT_DIR="$REPO/output"
METADATA="$SCRIPTS_DIR/metadata.yaml"
BOOK_MD="$BUILD_DIR/book.md"
OUTPUT_PDF="$OUTPUT_DIR/mmu-handbook.pdf"

# ── Argument parsing ───────────────────────────────────────────────────────────
QUICK=0
CHAPTERS_ARG="1-21"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --quick|-q)
            QUICK=1; shift ;;
        --chapters|-c)
            CHAPTERS_ARG="$2"; shift 2 ;;
        --help|-h)
            sed -n '3,18p' "$0" | sed 's/^#  \?//'
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Banner ─────────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "  MMU Handbook PDF build"
echo "  Chapters : $CHAPTERS_ARG"
echo "  Output   : $OUTPUT_PDF"
echo "════════════════════════════════════════════════════════════"

# ── Sanity checks ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not available." >&2; exit 1
fi
if ! docker image inspect pandoc/extra:latest &>/dev/null; then
    echo "Pulling pandoc/extra:latest …"
    docker pull pandoc/extra:latest
fi

# ── Step 1: preprocess (extract SVGs → PDFs, clean markdown) ──────────────────
if [[ $QUICK -eq 0 ]]; then
    echo
    echo "── Step 1/2: Preprocessing chapters ─────────────────────────────"

    # Check Python dependencies
    python3 -c "import cairosvg, bs4" 2>/dev/null || {
        echo "Installing Python dependencies …"
        pip3 install --quiet cairosvg beautifulsoup4
    }

    python3 "$SCRIPTS_DIR/preprocess.py" --chapters "$CHAPTERS_ARG"
else
    echo
    echo "── Step 1/2: Preprocessing skipped (--quick) ────────────────────"
    if [[ ! -f "$BOOK_MD" ]]; then
        echo "ERROR: $BOOK_MD not found. Run without --quick first." >&2
        exit 1
    fi
fi

# ── Step 2: pandoc compile via Docker ─────────────────────────────────────────
echo
echo "── Step 2/2: Compiling PDF via pandoc/extra Docker ──────────────"
echo "   Input    : $BOOK_MD"
echo "   Template : eisvogel (bundled in pandoc/extra)"
echo "   Engine   : xelatex"
echo

mkdir -p "$OUTPUT_DIR"

# We mount:
#   $SCRIPTS_DIR → /scripts   (source of book.md, metadata.yaml, figures)
#   $OUTPUT_DIR  → /output    (where the PDF is written)
#
# All paths inside the container are relative to /scripts.

docker run --rm \
    --volume "$SCRIPTS_DIR:/scripts:ro" \
    --volume "$OUTPUT_DIR:/output" \
    pandoc/extra:latest \
    /scripts/build/book.md \
    --metadata-file=/scripts/metadata.yaml \
    --from=markdown+raw_tex+fenced_divs+bracketed_spans \
    --to=pdf \
    --pdf-engine=xelatex \
    --template=eisvogel \
    --top-level-division=chapter \
    --toc \
    --toc-depth=2 \
    --number-sections=false \
    --syntax-highlighting=idiomatic \
    --resource-path=/scripts/build \
    --output=/output/mmu-handbook.pdf \
    2>&1 | grep -v '^$' | grep -v 'Missing character'   # suppress blank lines and font-missing noise

# ── Done ───────────────────────────────────────────────────────────────────────
echo
if [[ -f "$OUTPUT_PDF" ]]; then
    SIZE=$(du -sh "$OUTPUT_PDF" | cut -f1)
    echo "════════════════════════════════════════════════════════════"
    echo "  ✓  PDF built successfully"
    echo "     $OUTPUT_PDF  ($SIZE)"
    echo "════════════════════════════════════════════════════════════"
else
    echo "════════════════════════════════════════════════════════════"
    echo "  ✗  Build failed — no PDF produced.  Check output above."
    echo "════════════════════════════════════════════════════════════"
    exit 1
fi
