"""Small Win32 process layer: suspended launch, kill-on-close jobs and events.

Never signal by PID. PIDs are used only to open a query handle and compare its
creation time and image. Every child is assigned to its Job before execution.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import msvcrt
import os
from pathlib import Path
import subprocess

K = C.WinDLL("kernel32", use_last_error=True)


def api(name, result, *args):
    function = getattr(K, name)
    function.restype, function.argtypes = result, args
    return function


close_handle = api("CloseHandle", W.BOOL, W.HANDLE)
wait_handle = api("WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
create_mutex = api("CreateMutexW", W.HANDLE, C.c_void_p, W.BOOL, W.LPCWSTR)
release_mutex = api("ReleaseMutex", W.BOOL, W.HANDLE)
create_event = api("CreateEventW", W.HANDLE, C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR)
open_event = api("OpenEventW", W.HANDLE, W.DWORD, W.BOOL, W.LPCWSTR)
set_event = api("SetEvent", W.BOOL, W.HANDLE)
create_job = api("CreateJobObjectW", W.HANDLE, C.c_void_p, W.LPCWSTR)
set_job = api("SetInformationJobObject", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
assign_job = api("AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
terminate_job = api("TerminateJobObject", W.BOOL, W.HANDLE, W.UINT)
open_process = api("OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
process_times = api("GetProcessTimes", W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)] * 4))
process_image = api("QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
get_exit = api("GetExitCodeProcess", W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
resume_thread = api("ResumeThread", W.DWORD, W.HANDLE)
terminate_process = api("TerminateProcess", W.BOOL, W.HANDLE, W.UINT)


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                ("min_working", C.c_size_t), ("max_working", C.c_size_t), ("active", W.DWORD),
                ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", C.c_size_t),
                ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR), ("title", W.LPWSTR),
                *[(name, W.DWORD) for name in ("x", "y", "width", "height", "x_chars", "y_chars", "fill", "flags")],
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_bytes", C.c_void_p),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


create_process = api("CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p,
                     W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(StartupInfo), C.POINTER(ProcessInfo))


def checked(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


def name_for(root, purpose, token=""):
    digest = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode("utf-8")).hexdigest()[:24]
    return f"Global\\Yutaixian.{digest}.{purpose}.{token}"


class Handle:
    def __init__(self, handle):
        self.handle = checked(handle)

    def close(self):
        if self.handle:
            close_handle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Mutex(Handle):
    def __init__(self, name):
        super().__init__(create_mutex(None, False, name))
        result = wait_handle(self.handle, 0)
        if result not in (0, 0x80):  # abandoned mutex is recoverable
            self.close()
            raise RuntimeError("另一个启动、停止或管理操作正在进行。")

    def close(self):
        if self.handle:
            release_mutex(self.handle)
        super().close()


class Event(Handle):
    def __init__(self, name, *, existing=False):
        self.name = name
        super().__init__(open_event(0x100002, False, name) if existing else create_event(None, True, False, name))

    def set(self):
        checked(set_event(self.handle))

    def wait(self, milliseconds=0):
        result = wait_handle(self.handle, milliseconds)
        if result == 0xFFFFFFFF:
            checked(False)
        return result == 0


def process_identity(handle, pid):
    times = [W.FILETIME() for _ in range(4)]
    checked(process_times(handle, *(C.byref(value) for value in times)))
    size = W.DWORD(32768)
    image = C.create_unicode_buffer(size.value)
    checked(process_image(handle, 0, image, C.byref(size)))
    return {"pid": pid, "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "image": os.path.normcase(str(Path(image.value).resolve()))}


def identity(pid):
    with Handle(open_process(0x101000, False, pid)) as handle:
        return process_identity(handle.handle, pid)


def matches(record):
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int) or record["pid"] <= 0:
        return False
    try:
        with Handle(open_process(0x101000, False, record["pid"])) as handle:
            return wait_handle(handle.handle, 0) == 0x102 and process_identity(handle.handle, record["pid"]) == record
    except OSError:
        return False


class Process(Handle):
    def __init__(self, info):
        super().__init__(info.process)
        self.pid = info.pid
        self.identity = process_identity(self.handle, self.pid)

    def poll(self):
        if wait_handle(self.handle, 0) == 0x102:
            return None
        code = W.DWORD()
        checked(get_exit(self.handle, C.byref(code)))
        return code.value

    def wait(self, timeout=120):
        result = wait_handle(self.handle, int(timeout * 1000))
        if result == 0x102:
            raise TimeoutError("进程尚未退出")
        if result == 0xFFFFFFFF:
            checked(False)
        return self.poll()


class Job(Handle):
    def __init__(self, name):
        super().__init__(create_job(None, name))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            checked(set_job(self.handle, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def terminate(self):
        checked(terminate_job(self.handle, 1))

    def spawn(self, args, *, cwd, env, output):
        args = [str(arg) for arg in args]
        executable = str(Path(args[0]).resolve())
        command = C.create_unicode_buffer(subprocess.list2cmdline(args))
        environment = C.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda x: x[0].upper())) + "\0\0")
        startup, info = StartupInfo(), ProcessInfo()
        startup.cb, startup.flags = C.sizeof(startup), 0x101  # std handles + hidden window
        with open(os.devnull, "rb") as null:
            handles = [msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(output.fileno())]
            startup.stdin, startup.stdout, startup.stderr = handles[0], handles[1], handles[1]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            try:
                checked(create_process(executable, command, None, None, True,
                                       0x08000404, environment, str(cwd), C.byref(startup), C.byref(info)))
            finally:
                for handle in handles:
                    os.set_handle_inheritable(handle, False)
        try:
            checked(assign_job(self.handle, info.process))
            if resume_thread(info.thread) == 0xFFFFFFFF:
                checked(False)
            return Process(info)
        except BaseException:
            terminate_process(info.process, 1)  # handle to the child just created, never a stale PID
            close_handle(info.process)
            raise
        finally:
            close_handle(info.thread)
