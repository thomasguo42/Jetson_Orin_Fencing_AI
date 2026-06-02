"""Shared types."""

from dataclasses import dataclass, field
from enum import Enum


class MatchState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"  # 0x50 received, waiting for camera_ready
    PLAYING = "playing"      # camera_ready received, match in progress
    SETTLING = "settling"    # waiting for AI match_result


@dataclass
class Signal:
    fight: int       # hit-frame digit 1..9 (no 10; a tie is separate 8+9)
    source: str      # "hit" (ATxYZ frame); 0x52 lights are not stored here
    signal_ts: int   # corrected_ts = recv_ts + video_sync_offset_ms


def temp_result_from_lights(a_lit: bool, b_lit: bool) -> int:
    """Derive the json.txt temporary result code from 0x52 final lights (§13).

    Only A lit → 8; only B lit → 9; both lit → 10 (tie); neither → 0 (await AI).
    """
    if a_lit and not b_lit:
        return 8
    if b_lit and not a_lit:
        return 9
    if a_lit and b_lit:
        return 10
    return 0


@dataclass
class CurrentMatch:
    match_id: str = ""       # 0x50 receive timestamp ms
    state: MatchState = MatchState.IDLE
    begin_ts: int = 0        # camera_ready receive timestamp ms
    voice_end_ts: int = 0    # start.mp3 play complete timestamp ms
    weapon: int = 0          # from 0x50
    sensor: int = 0          # from 0x50
    signals: list[Signal] = field(default_factory=list)
    result_audio_announced: bool = False
    result_audio_done: bool = False
    ai_result_received: bool = False

    def reset(self):
        self.match_id = ""
        self.state = MatchState.IDLE
        self.begin_ts = 0
        self.voice_end_ts = 0
        self.weapon = 0
        self.sensor = 0
        self.signals.clear()
        self.result_audio_announced = False
        self.result_audio_done = False
        self.ai_result_received = False
