#!/usr/bin/env python3
"""Punct de intrare al aplicaţiei CatsSoft Downloader.

Rulare:  python main.py
"""
from catssoft.ui.main_window import DownloadManager
# ── orig:9894 ──

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DownloadManager()
    app.mainloop()
