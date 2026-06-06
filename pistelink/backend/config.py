"""Config loading from TOML with hot-reload for time-sensitive keys."""

import logging
import os
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/pistelink/config.toml"
logger = logging.getLogger(__name__)

DEFAULTS: dict[str, dict[str, Any]] = {
    "serial": {"device": "/dev/ttyUSB-mcu", "baud": 115200},
    "signal": {"video_sync_offset_ms": 0},
    "ai": {
        "enabled": True,
        "socket": "/run/pistelink/ai.sock",
        "reconnect_min_s": 1,
        "reconnect_max_s": 30,
        "heartbeat_s": 2,
        "heartbeat_timeout_s": 6,
        "result_timeout_s": 30,
    },
    "storage": {"root": "/var/lib/pistelink"},
    "audio": {"device": "default", "playback_timeout_s": 10},
    "upload": {
        "host": "",
        "port": 22,
        "username": "",
        "password": "",
        "private_key": "",      # path to SSH private key (public-key auth); takes precedence over password
        "key_passphrase": "",   # passphrase for the private key, if any
        "known_hosts": "",
        "base_path": "/",
        "timeout_s": 60,
        "post_upload_action": "delete_video_only",
    },
    "http": {"host": "127.0.0.1", "port": 8080},
    "ui": {"locale": "zh-CN"},
    "kiosk": {"enabled": False},
}


class Config:
    def __init__(self, path: str):
        self._path = path
        self._mtime: float = 0
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self):
        merged: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in DEFAULTS.items()
        }
        try:
            self._mtime = os.path.getmtime(self._path)
            with open(self._path, "rb") as f:
                file_data = tomllib.load(f)
            for section in merged:
                if section in file_data:
                    merged[section].update(file_data[section])
        except FileNotFoundError:
            if os.environ.get("PISTELINK_DEBUG") == "1":
                self._apply_debug_missing_file_defaults(merged)
                logger.warning(
                    "Config file not found: %s; using debug defaults rooted at %s",
                    self._path, merged["storage"]["root"])
            else:
                logger.warning("Config file not found: %s; using built-in defaults",
                               self._path)
        except OSError as e:
            logger.warning("Could not read config file %s: %s; using built-in defaults",
                           self._path, e)
        self._data = merged

    def _apply_debug_missing_file_defaults(self, merged: dict[str, dict[str, Any]]):
        root = os.path.dirname(os.path.abspath(self._path))
        if not root:
            return
        os.makedirs(root, exist_ok=True)
        merged["storage"]["root"] = root
        merged["ai"]["socket"] = os.path.join(root, "ai.sock")
        merged["audio"]["device"] = "default"

    def _check_reload(self):
        try:
            mtime = os.path.getmtime(self._path)
            if mtime > self._mtime:
                self.load()
        except OSError:
            pass

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))

    @property
    def video_sync_offset_ms(self) -> int:
        self._check_reload()
        return int(self.get("signal", "video_sync_offset_ms", 0))

    def reload_if_stale(self):
        self._check_reload()

    def write(self):
        dir_path = os.path.dirname(self._path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        lines = [_format_section(k, v) for k, v in self._data.items()]
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self._mtime = os.path.getmtime(self._path)

    def batch_update_and_write(self, updates: dict[str, dict[str, Any]]):
        """Apply multiple section updates then write once."""
        self._check_reload()
        for section, items in updates.items():
            if section in self._data:
                self._data[section].update(items)
            else:
                self._data[section] = dict(items)
        self.write()

    def to_dict(self) -> dict:
        return {k: dict(v) for k, v in self._data.items()}


def _format_section(name: str, items: dict) -> str:
    lines = [f"[{name}]"]
    for key, value in items.items():
        lines.append(f"{key} = {_format_value(value)}")
    lines.append("")
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f'"{str(value)}"'


_config: Config | None = None


def get_config(path: str | None = None) -> Config:
    global _config
    if _config is None:
        _config = Config(path or os.environ.get("PISTELINK_CONFIG", DEFAULT_CONFIG_PATH))
    return _config
