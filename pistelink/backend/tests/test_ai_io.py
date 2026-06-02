"""Unit tests for the AI UDS protocol layer (ai_io.py).

The Debug API bypasses the socket, so these are the only checks of NDJSON
framing, handshake, ping/pong and bad-frame handling. No real sockets are used:
the reader is a fed asyncio.StreamReader and the writer is a byte-collecting
fake — both transport-agnostic, so the tests run on Windows too.
"""

import asyncio
import json
import logging

from backend import ai_io
from backend.ai_io import AIClient


class FakeWriter:
    """Collects written bytes; supports the StreamWriter calls ai_io uses."""
    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def lines(self):
        text = bytes(self.buf).decode("utf-8")
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _recorder():
    events = []

    async def on_event(event_type, payload, match_id):
        events.append((event_type, payload, match_id))

    return events, on_event


def _line(obj):
    return json.dumps(obj).encode("utf-8") + b"\n"


# ── envelope framing ──────────────────────────────────────────────────────

def test_send_envelope_framing_and_id_increment():
    async def scenario():
        client = AIClient()
        fw = FakeWriter()
        client._writer = fw
        await client.send("voice_end", {"voice_end_ts": 5}, match_id="123")
        await client.send("ping")  # no payload, no match_id
        return fw.lines()

    lines = asyncio.run(scenario())
    first, second = lines[0], lines[1]
    assert first["v"] == 1
    assert first["type"] == "voice_end"
    assert first["id"] == 0
    assert isinstance(first["ts"], int)
    assert isinstance(first["ts_mono_ns"], int)   # optional monotonic envelope ts
    assert first["match_id"] == "123"
    assert first["payload"] == {"voice_end_ts": 5}
    assert second["id"] == 1                       # id increments per send
    assert "payload" not in second                 # empty payload omitted
    assert "match_id" not in second                # empty match_id omitted


# ── dispatch routing ──────────────────────────────────────────────────────

def test_ping_replies_pong_with_ping_id():
    async def scenario():
        client = AIClient()
        fw = FakeWriter()
        client._writer = fw
        await client._dispatch({"v": 1, "type": "ping", "id": 42})
        return fw.lines()

    lines = asyncio.run(scenario())
    assert lines[0]["type"] == "pong"
    assert lines[0]["payload"]["ref_id"] == 42   # references the ping id, not 0


def test_unknown_type_ignored():
    events, on_event = _recorder()

    async def scenario():
        client = AIClient(on_event=on_event)
        fw = FakeWriter()
        client._writer = fw
        await client._dispatch({"type": "frobnicate", "id": 5})
        return fw.lines()

    lines = asyncio.run(scenario())
    assert events == []      # not forwarded
    assert lines == []       # nothing sent


def test_known_events_forwarded_to_callback():
    events, on_event = _recorder()

    async def scenario():
        client = AIClient(on_event=on_event)
        await client._dispatch(
            {"type": "camera_ready", "id": 1, "match_id": "123",
             "payload": {"fps_nominal": 30.0}})

    asyncio.run(scenario())
    assert events == [("camera_ready", {"fps_nominal": 30.0}, "123")]


# ── handshake ─────────────────────────────────────────────────────────────
# StreamReader binds to the running loop at construction, so it is built and
# fed inside each scenario (not at module/function setup time).

def _run_connect(monkeypatch, fw, feed_lines, on_event=None):
    async def scenario():
        reader = asyncio.StreamReader()
        for ln in feed_lines:
            reader.feed_data(ln)

        async def fake_open(_path):
            return reader, fw
        monkeypatch.setattr(ai_io.asyncio, "open_unix_connection", fake_open,
                            raising=False)
        client = AIClient(on_event=on_event)
        await client._connect("/run/pistelink/ai.sock")
        return client

    return asyncio.run(scenario())


def test_handshake_success(monkeypatch):
    fw = FakeWriter()
    client = _run_connect(monkeypatch, fw, [
        _line({"v": 1, "type": "hello_ack", "id": 0, "payload": {"protocol_v": 1}})])
    assert client._handshake_done is True
    assert client.connected is True
    assert fw.lines()[0]["type"] == "hello"
    assert fw.lines()[0]["payload"]["protocol_v"] == 1


def test_handshake_version_mismatch_closes(monkeypatch):
    events, on_event = _recorder()
    fw = FakeWriter()
    client = _run_connect(monkeypatch, fw, [
        _line({"v": 1, "type": "hello_ack", "id": 0, "payload": {"protocol_v": 2}})],
        on_event=on_event)
    assert client._handshake_done is False
    assert client._fatal_protocol_error is True
    assert client.connected is False
    assert fw.closed is True
    assert any(e[0] == "ai_error" and e[1]["code"] == "E_AI_PROTO_VER"
               for e in events)


def test_run_does_not_retry_protocol_version_mismatch(monkeypatch):
    events, on_event = _recorder()
    calls = {"open": 0}

    class FakeConfig:
        def get(self, section, key):
            return {
                ("ai", "socket"): "/run/pistelink/ai.sock",
                ("ai", "reconnect_min_s"): 0.01,
                ("ai", "reconnect_max_s"): 0.01,
            }[(section, key)]

    async def fake_open(_path):
        calls["open"] += 1
        reader = asyncio.StreamReader()
        reader.feed_data(
            _line({"v": 1, "type": "hello_ack", "id": 0,
                   "payload": {"protocol_v": 2}})
        )
        reader.feed_eof()
        return reader, FakeWriter()

    async def scenario():
        monkeypatch.setattr(ai_io, "get_config", lambda: FakeConfig())
        monkeypatch.setattr(ai_io, "_socket_exists", lambda _path: True)
        monkeypatch.setattr(ai_io.asyncio, "open_unix_connection", fake_open,
                            raising=False)
        client = AIClient(on_event=on_event)
        await client.run()
        return client

    client = asyncio.run(scenario())
    assert calls["open"] == 1
    assert client._running is False
    assert any(e[0] == "ai_error" and e[1]["code"] == "E_AI_PROTO_VER"
               for e in events)


def test_run_waits_for_missing_socket_without_error_log(monkeypatch, caplog):
    class FakeConfig:
        def get(self, section, key):
            return {
                ("ai", "socket"): "/run/pistelink/ai.sock",
                ("ai", "reconnect_min_s"): 0.01,
                ("ai", "reconnect_max_s"): 0.01,
            }[(section, key)]

    async def fake_sleep(self, _seconds):
        self._running = False

    async def fake_open(_path):
        raise AssertionError("missing socket should be checked before connect")

    async def scenario():
        monkeypatch.setattr(ai_io, "get_config", lambda: FakeConfig())
        monkeypatch.setattr(ai_io, "_socket_exists", lambda _path: False)
        monkeypatch.setattr(AIClient, "_sleep", fake_sleep)
        monkeypatch.setattr(ai_io.asyncio, "open_unix_connection", fake_open,
                            raising=False)
        caplog.set_level(logging.DEBUG, logger=ai_io.__name__)
        client = AIClient()
        await client.run()

    asyncio.run(scenario())
    assert "AI socket not found" in caplog.text
    assert "AI socket error" not in caplog.text


def test_handshake_non_ack_first_packet_aborts(monkeypatch):
    fw = FakeWriter()
    client = _run_connect(monkeypatch, fw, [
        _line({"v": 1, "type": "match_result", "id": 0})])
    assert client._handshake_done is False
    assert client.connected is False


# ── bad-frame resilience ──────────────────────────────────────────────────

def test_bad_frame_does_not_drop_connection():
    events, on_event = _recorder()
    fw = FakeWriter()

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"{not valid json\n")  # malformed → E_AI_BAD_FRAME
        reader.feed_data(_line({"v": 1, "type": "camera_ready", "id": 1,
                                "payload": {}}))  # still processed after the bad one
        reader.feed_eof()
        client = AIClient(on_event=on_event)
        client._reader = reader
        client._writer = fw
        client._running = True
        await client._read_loop()

    asyncio.run(scenario())
    assert any(e[0] == "ai_error" and e[1]["code"] == "E_AI_BAD_FRAME"
               for e in events)
    assert any(e[0] == "camera_ready" for e in events)
