"""Translate PisteLink signal events into the legacy analyzer inputs.

The current low-latency analyzer still consumes the Arduino-era TXT event log.
This adapter keeps the compatibility layer explicit:

* PisteLink epoch-ms event timestamps are mapped to recorded video frames.
* A legacy TXT file is generated without frame tokens, matching the current
  parser in ``debug_referee_fps30.py``.
* A sidecar JSON file preserves the exact timestamp-to-frame mapping for audit.
"""

from __future__ import annotations

import bisect
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FrameTimestamp:
    frame: int
    ts: int
    mono_ns: Optional[int] = None


@dataclass(frozen=True)
class SignalFrameMapping:
    source: str
    fight: Optional[int]
    signal_ts: int
    signal_mono_ns: Optional[int]
    mapped_frame: Optional[int]
    mapped_frame_ts: Optional[int]
    delta_ms: Optional[int]
    mapping_mode: str


def load_frame_timestamps(path: Path) -> List[FrameTimestamp]:
    frames: List[FrameTimestamp] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            obj = json.loads(line)
            frames.append(
                FrameTimestamp(
                    frame=int(obj["frame"]),
                    ts=int(obj["ts"]),
                    mono_ns=int(obj["mono_ns"]) if obj.get("mono_ns") is not None else None,
                )
            )
    return frames


def write_frame_timestamps(path: Path, frames: Sequence[FrameTimestamp]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for frame in frames:
            handle.write(json.dumps(asdict(frame), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def write_mapping(path: Path, mappings: Sequence[SignalFrameMapping]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(mapping) for mapping in mappings]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def map_signal_to_frame(
    signal_ts: int,
    frames: Sequence[FrameTimestamp],
    mapping_mode: str = "nearest",
) -> SignalFrameMapping:
    if not frames:
        return SignalFrameMapping(
            source="unknown",
            fight=None,
            signal_ts=signal_ts,
            signal_mono_ns=None,
            mapped_frame=None,
            mapped_frame_ts=None,
            delta_ms=None,
            mapping_mode="no_frames",
        )

    timestamps = [frame.ts for frame in frames]
    insert_at = bisect.bisect_left(timestamps, signal_ts)
    if insert_at <= 0:
        chosen = frames[0]
    elif insert_at >= len(frames):
        chosen = frames[-1]
    elif mapping_mode == "previous":
        chosen = frames[insert_at - 1]
    elif mapping_mode == "next":
        chosen = frames[insert_at]
    else:
        before = frames[insert_at - 1]
        after = frames[insert_at]
        chosen = before if abs(signal_ts - before.ts) <= abs(signal_ts - after.ts) else after

    return SignalFrameMapping(
        source="unknown",
        fight=None,
        signal_ts=signal_ts,
        signal_mono_ns=None,
        mapped_frame=chosen.frame,
        mapped_frame_ts=chosen.ts,
        delta_ms=signal_ts - chosen.ts,
        mapping_mode=mapping_mode,
    )


def build_legacy_txt(
    *,
    match_id: str,
    side_map: Dict[str, str],
    signals: Sequence[Dict[str, Any]],
    terminal_signal: Optional[Dict[str, Any]],
    frames: Sequence[FrameTimestamp],
    match_begin_ts: Optional[int],
    voice_end_ts: Optional[int],
    mapping_mode: str = "nearest",
) -> Tuple[str, List[SignalFrameMapping]]:
    """Build the TXT consumed by the existing low-latency analyzer."""

    first_frame_ts = frames[0].ts if frames else (voice_end_ts or match_begin_ts or _first_signal_ts(signals) or _now_ms())
    active_start_ts = voice_end_ts or match_begin_ts or first_frame_ts
    lines: List[str] = []
    mappings: List[SignalFrameMapping] = []

    lines.append(f"Phrase {match_id} initialized at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"{_rel(active_start_ts, first_frame_ts):7.3f}s | Phrase recording started")
    lines.append(f"{_rel(active_start_ts, first_frame_ts):7.3f}s | PisteLink timestamp mapping enabled")

    seen_side_hits: Dict[str, int] = {}
    blade_contact_count = 0
    first_scoring_ts: Optional[int] = None

    for signal in signals:
        if signal.get("source") != "hit":
            continue
        signal_ts = _int_or_none(signal.get("signal_ts"))
        if signal_ts is None:
            continue

        fight = _int_or_none(signal.get("fight"))
        mapping = _mapping_for_signal(signal, frames, mapping_mode)
        mappings.append(mapping)
        rel = _rel(signal_ts, first_frame_ts)

        if fight == 3:
            blade_contact_count += 1
            lines.append(f"{rel:7.3f}s | Off-Target: Blade-to-blade contact.")
        elif fight in (8, 9):
            scoring_side = _fight_to_side(fight, side_map)
            if scoring_side is None:
                continue
            target_side = "right" if scoring_side == "left" else "left"
            seen_side_hits[scoring_side] = signal_ts
            first_scoring_ts = signal_ts if first_scoring_ts is None else min(first_scoring_ts, signal_ts)
            lines.append(
                f"{rel:7.3f}s | HIT: {scoring_side.title()} scores on {target_side.title()}!"
            )

    terminal_payload = terminal_signal or {}
    terminal_ts = _int_or_none(terminal_payload.get("signal_ts")) or first_scoring_ts or active_start_ts
    final_lights = _normalise_final_lights(terminal_payload.get("final_lights"), side_map)

    if terminal_signal is not None:
        mappings.append(_mapping_for_signal(terminal_signal, frames, mapping_mode))

    for side in ("left", "right"):
        if final_lights.get(side) and side not in seen_side_hits:
            target_side = "right" if side == "left" else "left"
            seen_side_hits[side] = terminal_ts
            first_scoring_ts = terminal_ts if first_scoring_ts is None else min(first_scoring_ts, terminal_ts)
            lines.append(
                f"{_rel(terminal_ts, first_frame_ts):7.3f}s | HIT: {side.title()} scores on {target_side.title()}!"
            )

    if seen_side_hits:
        lockout_ts = first_scoring_ts or terminal_ts
        lines.append(f"{_rel(lockout_ts, first_frame_ts):7.3f}s | Lockout period started (0.200s window)")

    if final_lights.get("left") and final_lights.get("right"):
        simultaneous_ts = max(seen_side_hits.values()) if seen_side_hits else terminal_ts
        lines.append(f"{_rel(simultaneous_ts, first_frame_ts):7.3f}s | Simultaneous valid hits detected.")

    lines.append(
        "Scores -> Fencer 1: {f1}, Fencer 2: {f2}".format(
            f1="HIT" if final_lights.get("right") else "MISS",
            f2="HIT" if final_lights.get("left") else "MISS",
        )
    )
    lines.append(f"{_rel(terminal_ts, first_frame_ts):7.3f}s | Phrase ended")
    lines.append(f"Blade contacts: {blade_contact_count}")
    return "\n".join(lines) + "\n", mappings


def _mapping_for_signal(
    signal: Dict[str, Any],
    frames: Sequence[FrameTimestamp],
    mapping_mode: str,
) -> SignalFrameMapping:
    signal_ts = _int_or_none(signal.get("signal_ts")) or _now_ms()
    base = map_signal_to_frame(signal_ts, frames, mapping_mode)
    return SignalFrameMapping(
        source=str(signal.get("source") or "unknown"),
        fight=_int_or_none(signal.get("fight")),
        signal_ts=signal_ts,
        signal_mono_ns=_int_or_none(signal.get("signal_mono_ns")),
        mapped_frame=base.mapped_frame,
        mapped_frame_ts=base.mapped_frame_ts,
        delta_ms=base.delta_ms,
        mapping_mode=base.mapping_mode,
    )


def _fight_to_side(fight: int, side_map: Dict[str, str]) -> Optional[str]:
    if fight == 8:
        return _clean_side(side_map.get("A"))
    if fight == 9:
        return _clean_side(side_map.get("B"))
    return None


def _normalise_final_lights(value: Any, side_map: Dict[str, str]) -> Dict[str, bool]:
    lights = {"left": False, "right": False}
    if not isinstance(value, dict):
        return lights

    for key in ("A", "B"):
        side = _clean_side(side_map.get(key))
        if side is not None:
            lights[side] = bool(value.get(key))

    for side in ("left", "right"):
        if side in value:
            lights[side] = bool(value.get(side))
    return lights


def _clean_side(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.lower() in {"left", "right"}:
        return value.lower()
    return None


def _rel(ts_ms: int, zero_ms: int) -> float:
    return max(0.0, (ts_ms - zero_ms) / 1000.0)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_signal_ts(signals: Iterable[Dict[str, Any]]) -> Optional[int]:
    for signal in signals:
        signal_ts = _int_or_none(signal.get("signal_ts"))
        if signal_ts is not None:
            return signal_ts
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
