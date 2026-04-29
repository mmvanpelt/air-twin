"""
update_fonts.py

Updates index.html Google Fonts import from Space Mono + DM Sans
to DM Sans + DM Mono for the new warm minimal design.

Run from project root:
    python update_fonts.py
"""

from pathlib import Path

ROOT = Path(__file__).parent
html_path = ROOT / "frontend" / "index.html"

src = html_path.read_text(encoding="utf-8")

old_font = '<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">'
new_font = '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;1,9..40,300&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">'

if "DM+Mono" in src:
    print("Font already updated")
elif old_font in src:
    src = src.replace(old_font, new_font)
    html_path.write_text(src, encoding="utf-8")
    print("Font updated — DM Sans + DM Mono")
else:
    # Try partial match
    src = src.replace(
        'family=Space+Mono',
        'family=DM+Mono:wght@400;500'
    )
    src = src.replace(
        'family=DM+Sans:wght@300;400;500',
        'family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600'
    )
    html_path.write_text(src, encoding="utf-8")
    print("Font updated via partial match")