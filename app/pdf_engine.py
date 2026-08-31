"""HTML-to-PDF conversion using an installed Edge or Chrome browser.

Ported from the desktop app's pdf_engine.py — this works great for now
because it's running on your Windows PC, which already has Edge installed.

IMPORTANT once this is hosted on a server (Render/Railway/etc.): most
servers don't have a browser installed, so this will need swapping for a
server-friendly renderer (WeasyPrint or a bundled headless Chromium via
Playwright) before PDF generation will work in production. Flagging this
now so it isn't a surprise at deploy time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def browser_candidates() -> list[Path]:
    candidates = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if not root:
            continue
        root = Path(root)
        candidates.extend([
            root / "Microsoft/Edge/Application/msedge.exe",
            root / "Google/Chrome/Application/chrome.exe",
        ])
    for command in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    unique = []
    for path in candidates:
        if path.is_file() and path not in unique:
            unique.append(path)
    return unique


def print_to_pdf(html_path: str | Path, pdf_path: str | Path) -> tuple[bool, str]:
    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    browsers = browser_candidates()
    if not browsers:
        return False, "Microsoft Edge or Google Chrome could not be found on this PC."
    errors = []
    for browser in browsers:
        with tempfile.TemporaryDirectory(prefix="tiger_one_pdf_") as profile:
            command = [
                str(browser), "--headless", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--print-to-pdf-no-header",
                f"--print-to-pdf={pdf_path}", html_path.as_uri(),
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            except Exception as exc:
                errors.append(f"{browser.name}: {exc}")
                continue
            if result.returncode == 0 and pdf_path.is_file() and pdf_path.stat().st_size > 1000:
                return True, str(pdf_path)
            errors.append(f"{browser.name}: {result.stderr.strip() or 'PDF was not created'}")
    return False, "\n".join(errors)
