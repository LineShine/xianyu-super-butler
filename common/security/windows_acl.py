"""Windows DACL protection for unattended instance secrets (no DPAPI)."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from pathlib import Path

ADV = C.WinDLL("advapi32", use_last_error=True)
KERNEL = C.WinDLL("kernel32", use_last_error=True)


def api(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, args
    return function


close = api(KERNEL, "CloseHandle", W.BOOL, W.HANDLE)
free = api(KERNEL, "LocalFree", W.HANDLE, W.HANDLE)
current = api(KERNEL, "GetCurrentProcess", W.HANDLE)
open_token = api(ADV, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
get_token = api(ADV, "GetTokenInformation", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
sid_string = api(ADV, "ConvertSidToStringSidW", W.BOOL, C.c_void_p, C.POINTER(W.LPWSTR))
convert_sd = api(ADV, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL, W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p)
set_security = api(ADV, "SetFileSecurityW", W.BOOL, W.LPCWSTR, W.DWORD, C.c_void_p)
get_security = api(ADV, "GetNamedSecurityInfoW", W.DWORD, W.LPWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p))
get_control = api(ADV, "GetSecurityDescriptorControl", W.BOOL, C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD))
get_ace = api(ADV, "GetAce", W.BOOL, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p))


def checked(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


def sid_text(sid):
    text = W.LPWSTR()
    checked(sid_string(sid, C.byref(text)))
    try:
        return text.value
    finally:
        free(C.cast(text, C.c_void_p))


def current_sid():
    token = W.HANDLE()
    checked(open_token(current(), 8, C.byref(token)))
    try:
        size = W.DWORD()
        get_token(token, 1, None, 0, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        checked(get_token(token, 1, buffer, size, C.byref(size)))
        return sid_text(C.cast(buffer, C.POINTER(C.c_void_p))[0])
    finally:
        close(token)


def protect(path: Path):
    """Replace the DACL, allowing only the current identity and SYSTEM."""
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        raise OSError("Refusing a reparse point for private storage")
    flags = "OICI" if path.is_dir() else ""
    descriptor = C.c_void_p()
    sddl = f"D:P(A;{flags};FA;;;{current_sid()})(A;{flags};FA;;;SY)"
    checked(convert_sd(sddl, 1, C.byref(descriptor), None))
    try:
        checked(set_security(str(path), 0x80000004, descriptor))
    finally:
        free(descriptor)


class ACL(C.Structure):
    _fields_ = [("revision", W.BYTE), ("reserved", W.BYTE), ("size", W.WORD),
                ("count", W.WORD), ("reserved2", W.WORD)]


def validate(path: Path):
    """Fail closed on a missing, inherited, broad or unsupported key DACL."""
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        raise OSError("Private storage is a reparse point")
    dacl, descriptor = C.c_void_p(), C.c_void_p()
    result = get_security(str(path), 1, 4, None, None, C.byref(dacl), None, C.byref(descriptor))
    if result:
        raise C.WinError(result)
    try:
        control, revision = W.WORD(), W.DWORD()
        checked(get_control(descriptor, C.byref(control), C.byref(revision)))
        if not dacl or not control.value & 0x1000:
            raise OSError("Private storage must have a protected DACL")
        allowed = {current_sid(), "S-1-5-18"}
        grants = set()
        acl = C.cast(dacl, C.POINTER(ACL)).contents
        for index in range(acl.count):
            ace = C.c_void_p()
            checked(get_ace(dacl, index, C.byref(ace)))
            header = C.string_at(ace, 4)
            # ACCESS_ALLOWED_ACE only; SID begins after header + access mask.
            sid = sid_text(ace.value + 8) if header[0] == 0 else None
            mask = W.DWORD.from_address(ace.value + 4).value
            if sid not in allowed or header[1] & 0x10 or mask != 0x1F01FF:
                raise OSError("Private storage grants access outside its owner and SYSTEM")
            grants.add(sid)
        if grants != allowed:
            raise OSError("Private storage is missing required access grants")
    finally:
        free(descriptor)
