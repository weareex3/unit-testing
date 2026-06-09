"""
Extract text from .pdf files in the implementation folder,
continuing the KB_ numbering series.
"""

import re
from pathlib import Path

SOURCE = Path(r"G:\My Drive\implementation")

def next_kb_number(folder):
    nums = []
    for f in folder.glob("KB_*.txt"):
        m = re.match(r"KB_(\d+)_", f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1 if nums else 1

def clean_name(stem):
    s = re.sub(r"[^\w]", "_", stem)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:60]

def already_extracted(folder, stem):
    target = clean_name(stem).lower()
    for f in folder.glob("KB_*.txt"):
        if target in f.name.lower():
            return True
    return False

def extract_pdf(path):
    import pdfplumber
    lines = [f"Source: {path.name}", ""]
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if text and text.strip():
                lines.append(f"--- Page {i} ---")
                lines.append(text.strip())
                lines.append("")
    return "\n".join(lines)

def run():
    counter = next_kb_number(SOURCE)
    pdfs = sorted(SOURCE.glob("*.pdf"))

    done = []
    skipped = []
    errors = []

    for path in pdfs:
        if already_extracted(SOURCE, path.stem):
            skipped.append(path.name)
            continue

        kb_name = f"KB_{counter:02d}_{clean_name(path.stem)}.txt"
        out_path = SOURCE / kb_name

        try:
            content = extract_pdf(path)
            if len(content.strip()) < 100:
                errors.append(f"  EMPTY {path.name} — skipped (no extractable text)")
                continue
            out_path.write_text(content, encoding="utf-8")
            done.append(f"  KB_{counter:02d} <- {path.name}")
            counter += 1
        except Exception as e:
            errors.append(f"  SKIP {path.name}: {e}")

    print(f"\nExtracted {len(done)} PDFs:")
    for d in done:
        print(d)

    if skipped:
        print(f"\nAlready extracted ({len(skipped)} skipped):")
        for s in skipped:
            print(f"  {s}")

    if errors:
        print(f"\nErrors/empty ({len(errors)}):")
        for e in errors:
            print(e)

    print(f"\nNext KB number: {counter}")

if __name__ == "__main__":
    run()
