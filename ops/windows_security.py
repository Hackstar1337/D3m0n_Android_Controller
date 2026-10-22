"""Owner-only Windows DACLs for private application state (stdlib only).

No shell, account-name lookup, inherited ACE retention or silent failure. POSIX
permissions are applied by platform_config on non-Windows hosts.
"""
from __future__ import annotations
import os
from pathlib import Path
import re


def _api():
    import ctypes
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.c_void_p]
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)]
    return ctypes, w, adv, kernel


def current_user_sid() -> str:
    if os.name != 'nt':
        raise OSError('Windows security API is unavailable on this host')
    c, w, adv, kernel = _api()
    token = w.HANDLE()
    text = w.LPWSTR()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, c.byref(token)):
            raise c.WinError(c.get_last_error())
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size.value, c.byref(size)):
            raise c.WinError(c.get_last_error())
        # TOKEN_USER begins with SID_AND_ATTRIBUTES; its first member is PSID.
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, c.byref(text)):
            raise c.WinError(c.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(c.cast(text, c.c_void_p))
        if token:
            kernel.CloseHandle(token)


def protect(path: Path, *, directory: bool) -> None:
    if os.name != 'nt':
        return
    c, w, adv, kernel = _api()
    descriptor = c.c_void_p()
    size = w.DWORD()
    flags = 'OICI' if directory else ''
    sddl = f'D:P(A;{flags};FA;;;{current_user_sid()})'
    try:
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), c.byref(size)):
            raise c.WinError(c.get_last_error())
        if not adv.SetFileSecurityW(str(Path(path)), 0x00000004 | 0x80000000, descriptor):
            raise c.WinError(c.get_last_error())
    except OSError as exc:
        raise PermissionError('Unable to apply an owner-only Windows state ACL') from exc
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)


def owner_only(path: Path) -> bool:
    """Check the real Windows ACL; do not infer privacy from POSIX mode bits."""
    if os.name != 'nt':
        return (Path(path).stat().st_mode & 0o077) == 0
    c, w, adv, kernel = _api()
    descriptor = c.c_void_p()
    text = w.LPWSTR()
    length = w.DWORD()
    try:
        result = adv.GetNamedSecurityInfoW(str(Path(path)), 1, 0x00000004, None, None, None, None, c.byref(descriptor))
        if result != 0:
            return False
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x00000004, c.byref(text), c.byref(length)):
            return False
        sddl = text.value or ''
        entries = re.findall(r'\(([^()]+)\)', sddl)
        sid = current_user_sid()
        return sddl.startswith('D:P') and bool(entries) and all(
            entry.split(';')[0] == 'A' and entry.split(';')[-1] == sid for entry in entries
        )
    finally:
        if text:
            kernel.LocalFree(c.cast(text, c.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)
