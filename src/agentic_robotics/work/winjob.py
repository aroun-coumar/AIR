"""Windows Job Objects through ``ctypes`` (standard library only).

What a job gives :class:`~agentic_robotics.work.sandbox.LocalProcessSandbox` on Windows:

* **memory**: ``JOB_OBJECT_LIMIT_JOB_MEMORY``, the committed memory of the whole job
  (the process and every descendant);
* **cpu**: ``JOB_OBJECT_LIMIT_JOB_TIME`` (total user-mode CPU time ``max_wall_s *
  max_cpus``) plus a hard CPU-rate cap of ``max_cpus`` cores where the OS supports it;
* **cleanup**: ``KILL_ON_JOB_CLOSE`` and ``TerminateJobObject`` kill descendants too when
  the run ends or times out.

The process is created suspended, assigned to the job, then resumed, so no descendant can
start outside the job. What a job does **not** give: a filesystem jail, network
isolation, or a different user. Those need a dedicated low-privilege account plus a
Windows Filtering Platform egress filter (the anthropics/sandbox-runtime approach), which
needs administrator setup and is left to the owner.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any

__requirements__ = ("AIR01", "REQ02", "G15")

CREATE_SUSPENDED = 0x00000004
_JobObjectExtendedLimitInformation = 9
_JobObjectCpuRateControlInformation = 15
_LIMIT_JOB_TIME = 0x00000004
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CPU_RATE_ENABLE = 0x1
_CPU_RATE_HARD_CAP = 0x4
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


def _kernel32() -> Any:
    k = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    H = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = H
    k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [H, H]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [H, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = H
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenThread.restype = H
    k.ResumeThread.argtypes = [H]
    k.ResumeThread.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [H]
    k.CloseHandle.restype = wintypes.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = H
    k.Thread32First.argtypes = [H, ctypes.POINTER(_ThreadEntry)]
    k.Thread32First.restype = wintypes.BOOL
    k.Thread32Next.argtypes = [H, ctypes.POINTER(_ThreadEntry)]
    k.Thread32Next.restype = wintypes.BOOL
    return k


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        _kernel32()
    except (OSError, AttributeError):
        return False
    return True


class JobError(OSError):
    pass


class Job:
    """One job per sandboxed run. ``applied`` lists the limits the OS accepted."""

    def __init__(self, memory_mb: int | None, cpus: float | None, wall_s: float) -> None:
        self._k = _kernel32()
        self.handle = self._k.CreateJobObjectW(None, None)
        if not self.handle:
            raise JobError(_last_error(), "CreateJobObjectW failed")
        self.applied: list[str] = []
        info = _ExtendedLimits()
        flags = _LIMIT_KILL_ON_JOB_CLOSE
        if memory_mb:
            flags |= _LIMIT_JOB_MEMORY
            info.JobMemoryLimit = memory_mb * 2**20
        if cpus:
            flags |= _LIMIT_JOB_TIME
            info.BasicLimitInformation.PerJobUserTimeLimit = int(wall_s * cpus * 10_000_000)
        info.BasicLimitInformation.LimitFlags = flags
        if not self._k.SetInformationJobObject(self.handle, _JobObjectExtendedLimitInformation,
                                               ctypes.byref(info), ctypes.sizeof(info)):
            err = _last_error()
            self.close()
            raise JobError(err, "SetInformationJobObject failed")
        self.applied.append("kill_on_close")
        if memory_mb:
            self.applied.append(f"job_memory={memory_mb}MiB")
        if cpus:
            self.applied.append(f"job_cpu_time={wall_s * cpus:g}s")
            rate = _CpuRate(_CPU_RATE_ENABLE | _CPU_RATE_HARD_CAP,
                            max(1, min(10_000, int(cpus / (os.cpu_count() or 1) * 10_000))))
            if self._k.SetInformationJobObject(self.handle, _JobObjectCpuRateControlInformation,
                                               ctypes.byref(rate), ctypes.sizeof(rate)):
                self.applied.append(f"cpu_rate_cap={cpus:g}cores")

    def adopt(self, pid: int) -> None:
        """Assign a *suspended* process to the job, then resume its threads."""
        proc = self._k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            raise JobError(_last_error(), "OpenProcess failed")
        try:
            if not self._k.AssignProcessToJobObject(self.handle, proc):
                raise JobError(_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._k.CloseHandle(proc)
        self._resume(pid)

    def _resume(self, pid: int) -> None:
        snap = self._k.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snap or snap == _INVALID_HANDLE:
            raise JobError(_last_error(), "CreateToolhelp32Snapshot failed")
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(_ThreadEntry)
            ok = self._k.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    th = self._k.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if th:
                        self._k.ResumeThread(th)
                        self._k.CloseHandle(th)
                ok = self._k.Thread32Next(snap, ctypes.byref(entry))
        finally:
            self._k.CloseHandle(snap)

    def terminate(self, code: int = 1) -> None:
        if self.handle:
            self._k.TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None
