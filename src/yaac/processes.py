"""The process tree, read three ways because the OSes keep it in three places.

Two questions are asked of it, and they cost very different amounts. Walking one's own line of descent is cheap
everywhere and happens on every hook event. Reading command lines is not -- Windows has no `ps` and no `/proc`,
and the Toolhelp snapshot carries an executable name but not the arguments, so the only way to the full line is
a CIM query through PowerShell, which costs a process launch. So the two are separate calls, and the expensive
one is made once and remembered.
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
"""Long enough for a PowerShell start on a loaded machine, and short enough that a wake is not held up by it."""

_lines: dict[int, str] | None = None


def parent(pid: int) -> int | None:
    """The parent of `pid`, or None where this platform will not say."""
    if sys.platform == "win32":
        return _parent_windows(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return int(stat.read().rpartition(")")[2].split()[1])
    except OSError, IndexError, ValueError:
        pass
    try:
        done = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except OSError, subprocess.SubprocessError:
        return None
    return int(done.stdout.strip()) if done.stdout.strip().isdigit() else None


def ancestry(depth: int = 12) -> list[int]:
    """This process and its forebears, nearest first. Empty beyond the first entry is an acceptable answer.

    A short list means this platform would not say who the parent is, which every caller treats as "found
    nothing" rather than as an error: a tie left unbroken is better than a tie broken wrongly.
    """
    line: list[int] = []
    current = os.getpid()
    while current and current not in line and len(line) < depth:
        line.append(current)
        current = parent(current) or 0
    return line


def command_lines() -> dict[int, str]:
    """Every process's full command line, by pid. Read once and remembered for the life of the process.

    A command line is how a process says what it was asked to do, and it is the only place some of that is
    written down -- which app-server a session is running under, for one. Nothing here parses it; callers match
    on what they are looking for.
    """
    global _lines
    if _lines is None:
        _lines = _read_command_lines()
    return _lines


def _read_command_lines() -> dict[int, str]:
    if sys.platform == "win32":
        # Win32_Process is the only place Windows keeps the arguments; Toolhelp has the executable name alone.
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }',
        ]
    else:
        command = ["ps", "-eo", "pid=,command="]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("could not read the process table: %s", exc)
        return {}

    lines: dict[int, str] = {}
    for row in (done.stdout or "").splitlines():
        pid, separator, rest = row.strip().partition("\t" if sys.platform == "win32" else " ")
        if separator and pid.isdigit():
            lines[int(pid)] = rest.strip()
    return lines


def _parent_windows(pid: int) -> int | None:
    """Windows keeps no `/proc` and its `ps` is not `ps`, so this reads the Toolhelp snapshot the API provides."""
    import ctypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        return None
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return None
        while entry.th32ProcessID != pid:
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return None
        return int(entry.th32ParentProcessID)
    finally:
        kernel32.CloseHandle(snapshot)
