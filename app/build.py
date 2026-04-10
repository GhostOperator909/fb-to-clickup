"""
PyInstaller build script for Ad Sync by AI Simple (macOS).

Usage:
    python app/build.py                # build, no signing
    python app/build.py --clean        # wipe build/ and dist/ first
    python app/build.py --sign         # build + sign with Developer ID
    python app/build.py --sign --pkg   # build + sign + wrap in signed .pkg
    python app/build.py --sign --notarize  # full pipeline (needs APPLE_ID env vars)

Notes:
    - Writable state lives in ~/Library/Application Support/AdSync — the
      .app bundle itself is read-only on client machines.
    - For signed builds, the Developer ID Application cert must be in your
      keychain. For notarization, set APPLE_ID, APPLE_TEAM_ID, and
      APPLE_APP_PASSWORD env vars (or pre-store a notarytool keychain profile
      named "adsync-notary").
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR  = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent

APP_NAME    = "Ad Sync by AI Simple"
BUNDLE_ID   = "com.aisimple.adsync"
ENTRY       = str(APP_DIR / "main.py")

# Code signing identities (must exist in your login keychain). These match
# the Apple Developer ID certs issued to AI Simple.
SIGN_IDENTITY_APP    = "Developer ID Application: Andrew Naegele (DTB456HJMJ)"
SIGN_IDENTITY_PKG    = "Developer ID Installer: Andrew Naegele (DTB456HJMJ)"
NOTARY_PROFILE       = "adsync-notary"  # `xcrun notarytool store-credentials adsync-notary`

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

def sign_app(app_path: Path) -> int:
    """
    Deep-sign every Mach-O binary inside the .app with the Developer ID
    Application identity, hardened runtime + entitlements, then sign the
    outer bundle.
    """
    entitlements = APP_DIR / "entitlements.plist"
    if not entitlements.exists():
        print(f"❌ Missing entitlements file: {entitlements}")
        return 1

    print(f"\n=== Signing {app_path.name} with Developer ID ===")
    # 1. Sign every Mach-O binary inside the bundle (bottom-up)
    targets = []
    for ext in ("*.dylib", "*.so"):
        targets.extend(app_path.rglob(ext))
    # Also pick up any executables in Contents/MacOS and Contents/Frameworks
    for sub in ("Contents/MacOS", "Contents/Frameworks", "Contents/Resources"):
        for p in (app_path / sub).rglob("*"):
            if p.is_file() and os.access(p, os.X_OK) and p.suffix not in (".py", ".pyc", ".html", ".css", ".js", ".png", ".icns", ".plist", ".txt", ".md", ".json"):
                targets.append(p)

    # Dedupe while preserving order
    seen = set()
    unique_targets = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique_targets.append(t)

    print(f"  Signing {len(unique_targets)} inner binaries...")
    for t in unique_targets:
        r = subprocess.run([
            "codesign", "--force", "--timestamp", "--options", "runtime",
            "--entitlements", str(entitlements),
            "--sign", SIGN_IDENTITY_APP,
            str(t),
        ], capture_output=True, text=True)
        if r.returncode != 0:
            # Some files (like text resources mistakenly marked executable) will
            # fail signing — skip them silently if codesign says "is not signable"
            if "is not signable" not in r.stderr and "not a Mach-O file" not in r.stderr:
                print(f"  ⚠ {t.relative_to(app_path)}: {r.stderr.strip()}")

    # 2. Sign the outer bundle
    print(f"  Signing outer bundle...")
    r = subprocess.run([
        "codesign", "--force", "--deep", "--timestamp", "--options", "runtime",
        "--entitlements", str(entitlements),
        "--sign", SIGN_IDENTITY_APP,
        str(app_path),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ Outer bundle sign failed:\n{r.stderr}")
        return r.returncode

    # 3. Verify the signature
    r = subprocess.run([
        "codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app_path),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ Signature verify failed:\n{r.stderr}")
        return r.returncode
    print(f"✓ Signature verified")
    print(r.stderr.strip())
    return 0

def build_pkg(app_path: Path) -> Path | None:
    """Wrap the signed .app in a signed .pkg installer."""
    pkg_path = REPO_DIR / "dist" / f"{APP_NAME}.pkg"
    print(f"\n=== Building signed .pkg ===")
    r = subprocess.run([
        "productbuild",
        "--component", str(app_path), "/Applications",
        "--sign", SIGN_IDENTITY_PKG,
        "--timestamp",
        str(pkg_path),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ pkg build failed:\n{r.stderr}")
        return None
    print(f"✓ {pkg_path}")
    return pkg_path

def notarize(target: Path) -> int:
    """Submit to Apple notarytool and staple the ticket on success."""
    print(f"\n=== Notarizing {target.name} (this can take 1-5 min) ===")
    # Use a stored keychain profile if available, otherwise fall back to env vars
    cmd = ["xcrun", "notarytool", "submit", str(target), "--wait"]
    if subprocess.run(["xcrun", "notarytool", "history", "--keychain-profile", NOTARY_PROFILE],
                      capture_output=True).returncode == 0:
        cmd += ["--keychain-profile", NOTARY_PROFILE]
    else:
        apple_id  = os.environ.get("APPLE_ID")
        team_id   = os.environ.get("APPLE_TEAM_ID")
        password  = os.environ.get("APPLE_APP_PASSWORD")
        if not (apple_id and team_id and password):
            print("❌ Notarization needs APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD env vars")
            print("   OR run once: xcrun notarytool store-credentials adsync-notary --apple-id ... --team-id ... --password ...")
            return 1
        cmd += ["--apple-id", apple_id, "--team-id", team_id, "--password", password]

    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0 or "status: Accepted" not in r.stdout:
        print(f"❌ Notarization failed:\n{r.stderr}")
        return 1

    print(f"  Stapling ticket...")
    r = subprocess.run(["xcrun", "stapler", "staple", str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ Staple failed:\n{r.stderr}")
        return r.returncode
    print(f"✓ Notarized and stapled")
    return 0

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

    sign_requested = "--sign" in sys.argv
    pkg_requested  = "--pkg"  in sys.argv
    notarize_requested = "--notarize" in sys.argv

    if sign_requested:
        if sign_app(app_path) != 0:
            return 1
        if notarize_requested:
            if notarize(app_path) != 0:
                return 1
        if pkg_requested:
            pkg = build_pkg(app_path)
            if not pkg:
                return 1
            if notarize_requested:
                if notarize(pkg) != 0:
                    return 1
        print()
        print(f"✓ Signed build ready: {app_path}")
    else:
        print()
        print("To launch:")
        print(f'  open "{app_path}"')
        print()
        print("This is an UNSIGNED build. macOS Gatekeeper will block it on")
        print("first run. Right-click → Open, then click Open in the dialog.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
