"""
fix_imports.py

Fixes all twin_engine import paths to use backend.twin_engine
when imported as part of the backend package.

Run from project root:
    python fix_imports.py
"""

from pathlib import Path

ROOT = Path(__file__).parent

# Files inside twin_engine package use relative imports
# Files outside twin_engine (brief_generator, main) use backend.twin_engine

TWIN_ENGINE_MODULES = [
    "backend/twin_engine/baseline.py",
    "backend/twin_engine/confidence.py",
    "backend/twin_engine/engine.py",
    "backend/twin_engine/events.py",
    "backend/twin_engine/filter.py",
    "backend/twin_engine/loader.py",
    "backend/twin_engine/performance.py",
    "backend/twin_engine/regime.py",
]

BACKEND_MODULES = [
    "backend/brief_generator.py",
    "backend/main.py",
]

changes = 0

# Inside twin_engine: from twin_engine.X → from backend.twin_engine.X
for rel_path in TWIN_ENGINE_MODULES:
    path = ROOT / rel_path
    if not path.exists():
        print(f"  SKIP (not found): {rel_path}")
        continue
    src = path.read_text(encoding="utf-8")
    new_src = src.replace("from twin_engine.", "from backend.twin_engine.")
    if new_src != src:
        path.write_text(new_src, encoding="utf-8")
        count = src.count("from twin_engine.")
        print(f"  ✓ {rel_path} — {count} import(s) updated")
        changes += count
    else:
        print(f"  – {rel_path} — already correct")

# Outside twin_engine: same fix
for rel_path in BACKEND_MODULES:
    path = ROOT / rel_path
    if not path.exists():
        print(f"  SKIP (not found): {rel_path}")
        continue
    src = path.read_text(encoding="utf-8")
    new_src = src.replace("from twin_engine.", "from backend.twin_engine.")
    if new_src != src:
        path.write_text(new_src, encoding="utf-8")
        count = src.count("from twin_engine.")
        print(f"  ✓ {rel_path} — {count} import(s) updated")
        changes += count
    else:
        print(f"  – {rel_path} — already correct")

print(f"\nDone — {changes} import(s) updated across all files")
print("\nNow run:")
print("  python -m backend.main")
