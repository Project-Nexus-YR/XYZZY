"""Finding 3 (high): a chunked body carries no Content-Length to lie about,
so the only place a size cap can hold is the ASGI layer itself, counting the
bytes a request actually delivers rather than trusting a header.

This runs against a real server in its own process, the way the finding's own
reproduction did: an in-process TestClient cannot show a memory blow-up, since
whatever it "receives" is already sitting in the test's own memory. A chunked
POST with no Content-Length and no Authorization header is sent over a raw
socket (so nothing in the test's own HTTP client could add one), and the
server process's peak working set is read directly from the OS rather than
guessed at.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC_ROOT = str(Path(__file__).resolve().parents[2] / "src")

_SERVER_SCRIPT = """
import os
import uvicorn
from multiplayer.server import create_app

app = create_app(":memory:", auth_tokens={"owner-token": "user_1"})
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["FIX_API_TEST_PORT"]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_ready(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                sock.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                if sock.recv(64):
                    return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("server never became ready")


def _peak_working_set_bytes(pid: int) -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_information | process_vm_read, False, pid
        )
        if not handle:
            raise OSError("could not open the server process to read its memory")
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if not ok:
                raise OSError("GetProcessMemoryInfo failed")
            return int(counters.PeakWorkingSetSize)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    with open(f"/proc/{pid}/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    raise OSError("VmHWM not found")


def _send_chunked_body(port: int, total_bytes: int, chunk_size: int = 8_192) -> bytes:
    """A hand-built chunked POST: no Content-Length, no Authorization, exactly
    what an unauthenticated attacker sending gigabytes needs, and nothing an
    httpx/requests client could quietly turn into a declared length instead.

    Each wire chunk stays under h11's own ``max_incomplete_event_size``
    (16 KiB by default): a bigger one risks h11 answering its own 400 while a
    single chunk is still arriving, which would prove nothing about this
    fix either way.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.settimeout(20)
        header = (
            "POST /api/v1/organizations HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(header)

        # Read concurrently with sending, on its own thread: the server closes
        # the connection right after answering, and on loopback that close can
        # race a still-sending client hard enough to reset the connection
        # before a request-then-recv script ever gets to look — a hard reset
        # can arrive before bytes already written are ever read, whatever
        # order they left the server in. A reader that is always blocked in
        # recv() sees the response the moment it is written, independent of
        # whatever the send side is doing when the reset lands.
        response = bytearray()
        read_done = threading.Event()

        def _read_response() -> None:
            try:
                while len(response) < 4096:
                    data = sock.recv(4096)
                    if not data:
                        break
                    response.extend(data)
                    if b"\r\n\r\n" in response:
                        break
            except OSError:
                pass
            finally:
                read_done.set()

        reader = threading.Thread(target=_read_response, daemon=True)
        reader.start()

        chunk = b"x" * chunk_size
        sent = 0
        try:
            while sent < total_bytes and not read_done.is_set():
                this = min(chunk_size, total_bytes - sent)
                sock.sendall(f"{this:x}\r\n".encode("ascii") + chunk[:this] + b"\r\n")
                sent += this
            else:
                if not read_done.is_set():
                    sock.sendall(b"0\r\n\r\n")
        except OSError:
            # The server answered and closed before the whole body went out —
            # exactly the point being proven; the reader thread has whatever
            # came back.
            pass

        read_done.wait(timeout=15)
        try:
            sock.close()
        except OSError:
            pass
        return bytes(response)


@pytest.fixture
def live_server() -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    import os

    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_ROOT
    env["FIX_API_TEST_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_ready(port, time.monotonic() + 20)
        yield proc, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_a_chunked_64mib_body_is_capped_at_the_asgi_layer_with_flat_rss(live_server) -> None:
    proc, port = live_server
    baseline = _peak_working_set_bytes(proc.pid)

    started = time.monotonic()
    response = _send_chunked_body(port, total_bytes=64 * 1024 * 1024)
    elapsed = time.monotonic() - started

    status_line = response.split(b"\r\n", 1)[0] if response else b""
    assert b"413" in status_line, f"expected a 413 status line, got {status_line!r}"
    # "within seconds": generous for a loaded CI box, but nowhere near what
    # streaming all 64 MiB before answering would take.
    assert elapsed < 15, f"the 413 took {elapsed:.1f}s — the body was read in full first"

    peak = _peak_working_set_bytes(proc.pid)
    grew_by = peak - baseline
    # Nowhere near the 64 MiB sent, or even the default 1 MiB cap read once:
    # a process that buffered the request would grow by tens of megabytes.
    assert grew_by < 32 * 1024 * 1024, (
        f"server process grew by {grew_by / 1024 / 1024:.1f} MiB answering a "
        "64 MiB chunked body — it is buffering past the cap"
    )
