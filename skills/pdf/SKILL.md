# PDF Skill

Use this skill when a task involves reading, creating, or reviewing PDFs where layout matters.

## Workflow

1. Prefer visual review by rendering pages to images and inspecting them.
2. Use `reportlab` for PDF generation when you need deterministic layout.
3. Use `pdfplumber` or `pypdf` for extraction and quick checks, but do not trust extracted text for layout fidelity.
4. After each meaningful change, re-render and verify spacing, clipping, pagination, and legibility.

## Dependencies

Prefer `uv`:

```bash
uv pip install reportlab pdfplumber pypdf
```

If `uv` is unavailable:

```bash
python3 -m pip install reportlab pdfplumber pypdf
```

Rendering usually requires Poppler:

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install -y poppler-utils
```

## Paths

- Put temporary files in a task-local temp directory such as `tmp/` inside the current project, not in a hard-coded repo path.
- Write final outputs to the user-requested path or to a clearly named project-local location.
- Keep filenames stable and descriptive so repeated render-check cycles are easy to follow.

## Rendering command

```bash
pdftoppm -png $INPUT_PDF $OUTPUT_PREFIX
```

## Quality rules

- Maintain consistent typography, spacing, margins, and section hierarchy.
- Avoid clipped text, overlaps, unreadable glyphs, and blurry tables or charts.
- Use ASCII hyphens only.
- Do not deliver until the latest image inspection shows zero visual or formatting defects.
