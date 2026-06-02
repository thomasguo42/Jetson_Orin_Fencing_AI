"""Audio playback — subprocess on Linux, MCI (winmm) on Windows."""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import get_config

if sys.platform == "win32":
    import ctypes

logger = logging.getLogger(__name__)

SOUND_DIR = Path(os.environ.get("PISTELINK_SOUND_DIR", "sound"))

_PLAYER_ARGS: dict[str, list[str]] = {
    "mpg123": ["mpg123", "-q"],
    "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    "gst-play-1.0": ["gst-play-1.0", "--quiet"],
}

_MCI_ALIAS = "pistelink_audio"


def _find_player() -> str | None:
    """Return a player name for Linux, or None on Windows (uses MCI instead)."""
    if sys.platform == "win32":
        return None  # Windows uses _mci_play, not subprocess
    for name in ("mpg123", "ffplay", "gst-play-1.0"):
        if shutil.which(name):
            return name
    return None


def _mci_play(path: str):
    """Play an MP3 synchronously via Windows MCI (winmm.dll). Called in executor."""
    mci = ctypes.windll.winmm.mciSendStringW
    escaped = path.replace('"', '""')
    r = mci(f'open "{escaped}" type mpegvideo alias {_MCI_ALIAS}', None, 0, 0)
    if r != 0:
        logger.error("MCI open error: %d (path=%s)", r, path)
        return
    mci(f"play {_MCI_ALIAS} wait", None, 0, 0)
    mci(f"close {_MCI_ALIAS}", None, 0, 0)


def _mci_stop():
    """Stop MCI playback (called from clear() on match cancel)."""
    ctypes.windll.winmm.mciSendStringW(f"stop {_MCI_ALIAS}", None, 0, 0)
    ctypes.windll.winmm.mciSendStringW(f"close {_MCI_ALIAS}", None, 0, 0)


class AudioPlayer:
    def __init__(self, on_play_done=None):
        self._on_play_done = on_play_done  # async callable(filename)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._player: str | None = None
        self._current_proc: asyncio.subprocess.Process | None = None
        self._mci_playing = False

    async def run(self):
        self._player = _find_player()
        if self._player is None and sys.platform != "win32":
            logger.warning(
                "No audio player found. Playing silently — "
                "callbacks still fire so match finalization is unaffected.")
        else:
            logger.info("Audio player: %s",
                        self._player or "MCI (Windows)")
        self._running = True

        while self._running:
            filename = await self._queue.get()
            if filename is None:
                break

            await self._play_blocking(filename)

            if self._on_play_done:
                await self._on_play_done(filename)

    async def _play_blocking(self, filename: str):
        """Play one file and wait for it to finish. Never raises."""
        path = (SOUND_DIR / filename).resolve()
        if not path.exists():
            logger.warning("Audio file missing: %s", path)
            return

        if sys.platform == "win32":
            await self._play_mci(str(path))
        elif self._player is None:
            return  # no player: treat as instantly "done"
        else:
            await self._play_subprocess(str(path))

    async def _play_mci(self, path: str):
        """Play via Windows MCI in a thread executor."""
        self._mci_playing = True
        try:
            await asyncio.get_event_loop().run_in_executor(None, _mci_play, path)
        except Exception as e:
            logger.error("MCI playback error: %s", e)
        finally:
            self._mci_playing = False

    def _build_cmd(self, path: str) -> list[str]:
        """Build the player command, forcing ALSA output to the configured
        device when one is set.

        Running as a systemd system service there is no user session and thus no
        PulseAudio: mpg123's default (pulse → alsa default) lands on HDMI and is
        silent on an appliance with a USB speaker. Setting `[audio] device` to an
        ALSA device (e.g. "plughw:2,0") routes playback directly to the hardware,
        independent of any desktop session. "default"/empty keeps the player's own
        default (used on dev hosts where PulseAudio is available).
        """
        cmd = list(_PLAYER_ARGS[self._player])
        device = (get_config().get("audio", "device", "default") or "").strip()
        if device and device != "default" and self._player == "mpg123":
            cmd += ["-o", "alsa", "-a", device]
        elif device and device != "default" and self._player == "gst-play-1.0":
            if device.startswith("pulse:"):
                pulse_sink = device.removeprefix("pulse:")
                cmd += ["--audiosink", f"pulsesink device={pulse_sink}"]
            else:
                cmd += ["--audiosink", f"alsasink device={device}"]
        return cmd + [path]

    async def _play_subprocess(self, path: str):
        """Play via external subprocess (Linux)."""
        cmd = self._build_cmd(path)
        logger.info("Audio play: %s", " ".join(cmd))
        try:
            self._current_proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            _stdout, stderr = await self._current_proc.communicate()
            stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            if self._current_proc.returncode != 0 or "ERROR" in stderr_text:
                logger.error(
                    "Audio playback failed rc=%s: %s",
                    self._current_proc.returncode,
                    stderr_text or "(no stderr)",
                )
            elif stderr_text:
                logger.warning("Audio playback warning: %s", stderr_text)
        except Exception as e:
            logger.error("Audio playback error: %s", e)
        finally:
            self._current_proc = None

    def play(self, filename: str):
        self._queue.put_nowait(filename)

    def clear(self):
        """Cancel current playback and drain queued audio. Used on match cancel."""
        if self._current_proc is not None:
            try:
                self._current_proc.kill()
            except Exception:
                pass
            self._current_proc = None
        if self._mci_playing:
            try:
                _mci_stop()
            except Exception:
                pass
            self._mci_playing = False
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def stop(self):
        self._running = False
        self.clear()
        self._queue.put_nowait(None)
