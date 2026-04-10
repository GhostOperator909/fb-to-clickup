"""
PyInstaller build script for Ad Sync by AI Simple (macOS).

Usage:
    python app/build.py           # unsigned dev build
    python app/build.py --clean   # wipe build/ and dist/ first

Output:
    dist/Ad Sync by AI Simple.app

Notes:
    - PyInstaller bundles a shared libpython rather than a full interpreter,
      so `main.py` must NOT subprocess-call python. Sync logic is imported
      directly by `executions.sync_engine`.
    - Writable state lives in ~/Library/Application Support/AdSync — the
      .app bundle itself is read-only on client machines.
    - This is an UNSIGNED build. For client distribution the resulting .app
      must be signed with a Developer ID Application cert, notarized via
      notarytool, and packaged in a signed pkg. See HANDOFF_CODESIGN.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR  = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent

APP_NAME    = "Ad Sync by AI Simple"
BUNDLE_ID   = "com.aisimple.adsync"
ENTRY       = str(APP_DIR / "main.py")

def clean() -> None:
    for d in ("build", "dist"):
        p = REPO_DIR / d
        if p.exists():
            print(f"  removing {p}")
            shutil.rmtree(p)
    for spec in REPO_DIR.glob("*.spec"):
        print(f"  removing {spec.name}")
        spec.unlink()

def build() -> int:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--windowed",
        "--noconfirm",
        "--osx-bundle-identifier", BUNDLE_ID,
        # The UI files must be shipped alongside main.py
        "--add-data", f"{APP_DIR / 'ui'}:app/ui",
        # The sync engine + directives + requirements ride along as data
        "--add-data", f"{REPO_DIR / 'executions'}:executions",
        "--add-data", f"{REPO_DIR / 'directives'}:directives",
        "--add-data", f"{REPO_DIR / 'requirements.txt'}:.",
        # Make PyInstaller analyze executions.sync_engine so its transitive
        # imports (difflib, etc.) get bundled — not just the .py file.
        "--paths", str(REPO_DIR),
        "--hidden-import", "executions",
        "--hidden-import", "executions.sync_engine",
        "--hidden-import", "difflib",
        # Hidden imports pywebview + nacl sometimes miss
        "--hidden-import", "webview",
        "--hidden-import", "webview.platforms.cocoa",
        "--hidden-import", "nacl",
        "--hidden-import", "nacl.public",
        "--hidden-import", "nacl.encoding",
        "--hidden-import", "nacl.bindings",
        "--hidden-import", "dotenv",
        "--hidden-import", "requests",
    ]

    icon = APP_DIR / "icon.icns"
    if icon.exists():
        cmd += ["--icon", str(icon)]

    cmd.append(ENTRY)

    print("Running PyInstaller:")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_DIR)
    return result.returncode

def main() -> int:
    if "--clean" in sys.argv:
        clean()
    rc = build()
    if rc != 0:
        print(f"\nBuild failed with exit code {rc}")
        return rc
    app_path = REPO_DIR / "dist" / f"{APP_NAME}.app"
    print()
    print("=" * 60)
    print(f"Build complete: {app_path}")
    print("=" * 60)
    print()
    print("To launch:")
    print(f'  open "{app_path}"')
    print()
    print("This is an UNSIGNED build. macOS Gatekeeper will block it on")
    print("first run. Right-click → Open, then click Open in the dialog.")
    print("For signed/notarized client distribution, see HANDOFF_CODESIGN.md.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
