"""core/config — Setările aplicației."""

import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno

from catssoft.core.database import DatabaseManager
# ── orig:2840 ──

# ─── Config Manager ───────────────────────────────────────────────────────────
class ConfigManager:
    DEFAULTS = {
        "max_concurrent": 3,
        "queue_profile": "Balanced",
        "default_folder": os.path.expanduser("~/Downloads"),
        "speed_limit": 0,
        "theme_name": "Dark Modern",
        "theme_mode": "",
        "accent_color": "#3b82f6",
        "notifications": True,
        "clear_on_exit": False,
        "auto_sort": True,
        "clipboard_monitor": True,
        "av_scan": False,
        "segments_default": 1,
        "minimize_to_tray": True,
        "animations": True,
        "smart_retry_enabled": True,
        "smart_retry_max_attempts": 2,
        "smart_retry_base_delay": 5,
        "smart_queue_aging_enabled": True,
        "smart_queue_aging_minutes": 20,
        "smart_disk_guard_enabled": False,
        "smart_disk_min_free_mb": 512,
        "adaptive_streams_enabled": True,
        "adaptive_stream_max": 16,
        "endgame_enabled": True,
        "endgame_trigger_seconds": 4,
        "multi_network_enabled": False,
        "multi_network_selected": [],
        "network_speed_limits": {},
        "network_usage": {},
        "slow_mode_enabled": False,
        "slow_mode_limit_kbps": 512,
        "proxy_url": "",
    }

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.config = self.load()

    def load(self):
        """Overlay the persisted values on the defaults with a single query."""
        config = dict(self.DEFAULTS)
        stored = self.db.get_all_settings()
        for key in config:
            if key in stored:
                config[key] = stored[key]
        config["theme_mode"] = str(config.get("theme_mode") or "").lower()
        return config

    def save(self):
        self.db.save_settings_batch(list(self.config.items()))

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.db.save_setting(key, value)

    def set_many(self, items):
        """Update and persist a group of settings in a single transaction."""
        items = dict(items)
        if not items:
            return
        self.config.update(items)
        self.db.save_settings_batch(list(items.items()))
