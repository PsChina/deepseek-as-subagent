"""Strict owner/DACL validation for private Windows file handles."""
from __future__ import annotations

import os


class WindowsAclError(RuntimeError):
    pass


if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix
    import ctypes
    from ctypes import wintypes

    _ADVAPI = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
    _GET_SECURITY = _ADVAPI.GetSecurityInfo
    _GET_SECURITY.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    _GET_SECURITY.restype = wintypes.DWORD
    _GET_ACE = _ADVAPI.GetAce
    _GET_ACE.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    )
    _GET_ACE.restype = wintypes.BOOL
    _EQUAL_SID = _ADVAPI.EqualSid
    _EQUAL_SID.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _EQUAL_SID.restype = wintypes.BOOL
    _CREATE_SID = _ADVAPI.CreateWellKnownSid
    _CREATE_SID.argtypes = (
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    )
    _CREATE_SID.restype = wintypes.BOOL
    _OPEN_TOKEN = _ADVAPI.OpenProcessToken
    _OPEN_TOKEN.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    _OPEN_TOKEN.restype = wintypes.BOOL
    _TOKEN_INFO = _ADVAPI.GetTokenInformation
    _TOKEN_INFO.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _TOKEN_INFO.restype = wintypes.BOOL
    _GET_PROCESS = _KERNEL.GetCurrentProcess
    _GET_PROCESS.restype = wintypes.HANDLE
    _CLOSE_HANDLE = _KERNEL.CloseHandle
    _CLOSE_HANDLE.argtypes = (wintypes.HANDLE,)
    _CLOSE_HANDLE.restype = wintypes.BOOL
    _LOCAL_FREE = _KERNEL.LocalFree
    _LOCAL_FREE.argtypes = (ctypes.c_void_p,)
    _LOCAL_FREE.restype = ctypes.c_void_p

    class _Acl(ctypes.Structure):
        _fields_ = (
            ("revision", wintypes.BYTE), ("sbz1", wintypes.BYTE),
            ("size", wintypes.WORD), ("ace_count", wintypes.WORD),
            ("sbz2", wintypes.WORD),
        )

    class _AceHeader(ctypes.Structure):
        _fields_ = (
            ("ace_type", wintypes.BYTE), ("ace_flags", wintypes.BYTE),
            ("ace_size", wintypes.WORD),
        )

    class _AllowedAce(ctypes.Structure):
        _fields_ = (
            ("header", _AceHeader), ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        )

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class _TokenUser(ctypes.Structure):
        _fields_ = (("user", _SidAndAttributes),)

    class _TokenOwner(ctypes.Structure):
        _fields_ = (("owner", ctypes.c_void_p),)


_SE_FILE_OBJECT = 1
_OWNER_DACL = 0x1 | 0x4
_TOKEN_QUERY, _TOKEN_USER, _TOKEN_OWNER = 0x8, 1, 4
_ALLOW_ACE, _INHERIT_ONLY = 0, 0x8
_ALLOW_TYPES = frozenset({0, 4, 5, 9, 11})
# Creator Owner, Local System, Builtin Administrators, Creator Owner Rights.
_SAFE_WELL_KNOWN = (3, 22, 26, 71)
_MAX_SID = 68
_MUTATING_ACCESS = (
    0x00000002 | 0x00000004 | 0x00000010 | 0x00000040 | 0x00000100
    | 0x00010000 | 0x00040000 | 0x00080000 | 0x10000000 | 0x40000000
)


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsAclError("Windows ACL validation is unavailable")


def _current_token_sid(info_class: int) -> tuple[object, int]:
    _require_windows()
    token = wintypes.HANDLE()
    if not _OPEN_TOKEN(_GET_PROCESS(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        _TOKEN_INFO(token, info_class, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not _TOKEN_INFO(token, info_class, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info_class == _TOKEN_USER:
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        elif info_class == _TOKEN_OWNER:
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenOwner)).contents.owner
        else:  # Defensive: this helper only understands SID-bearing token classes.
            raise WindowsAclError("unsupported Windows token SID information class")
        if not sid:
            raise WindowsAclError("Windows access token returned an empty SID")
        return buffer, int(sid)
    finally:
        if not _CLOSE_HANDLE(token):
            raise ctypes.WinError(ctypes.get_last_error())


def _current_user_sid() -> tuple[object, int]:
    return _current_token_sid(_TOKEN_USER)


def _current_owner_sid() -> tuple[object, int]:
    """Return the access token's default owner SID for newly created objects."""
    return _current_token_sid(_TOKEN_OWNER)


def _well_known_sid(kind: int) -> tuple[object, int]:
    buffer = ctypes.create_string_buffer(_MAX_SID)
    size = wintypes.DWORD(len(buffer))
    if not _CREATE_SID(kind, None, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer, ctypes.addressof(buffer)


def _allowed_sids() -> tuple[list[object], list[int]]:
    current_buffer, current = _current_user_sid()
    buffers: list[object] = [current_buffer]
    pointers = [current]
    for kind in _SAFE_WELL_KNOWN:
        buffer, pointer = _well_known_sid(kind)
        buffers.append(buffer)
        pointers.append(pointer)
    return buffers, pointers


def _sid_is_allowed(sid: int, allowed: list[int]) -> bool:
    return any(_EQUAL_SID(sid, candidate) for candidate in allowed)


def _owner_sid_is_allowed(
    owner: int,
    current_user: int,
    token_owner: int,
    allowed: list[int],
) -> bool:
    """Accept the user owner, or the token's default owner when already trusted."""
    if _EQUAL_SID(owner, current_user):
        return True
    return bool(
        _EQUAL_SID(owner, token_owner)
        and _sid_is_allowed(token_owner, allowed)
    )


def _ace_applies_to_object(flags: int) -> bool:
    return not bool(flags & _INHERIT_ONLY)


def _validate_aces(dacl: int, allowed: list[int]) -> None:
    acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
    for index in range(acl.ace_count):
        pointer = ctypes.c_void_p()
        if not _GET_ACE(dacl, index, ctypes.byref(pointer)):
            raise ctypes.WinError(ctypes.get_last_error())
        header = ctypes.cast(pointer, ctypes.POINTER(_AceHeader)).contents
        mask = ctypes.c_uint32.from_address(pointer.value + 4).value
        if (
            header.ace_type not in _ALLOW_TYPES
            or not _ace_applies_to_object(header.ace_flags)
        ):
            continue
        if not mask & _MUTATING_ACCESS:
            continue
        if header.ace_type != _ALLOW_ACE:
            raise WindowsAclError("unsupported write-capable Windows ACL entry")
        offset = _AllowedAce.sid_start.offset
        if not _sid_is_allowed(pointer.value + offset, allowed):
            raise WindowsAclError(
                "Windows path grants mutation access to another principal"
            )


def validate_private_handle(handle: int) -> None:
    """Require a trusted token owner and no foreign write-capable allow ACE."""
    _require_windows()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = _GET_SECURITY(
        handle, _SE_FILE_OBJECT, _OWNER_DACL, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if code:
        raise ctypes.WinError(code)
    try:
        buffers, allowed = _allowed_sids()
        token_owner_buffer, token_owner = _current_owner_sid()
        if (
            not owner.value
            or not _owner_sid_is_allowed(
                owner.value, allowed[0], token_owner, allowed
            )
        ):
            raise WindowsAclError(
                "Windows path is not owned by the current user or trusted token owner"
            )
        if not dacl.value:
            raise WindowsAclError("Windows path has an unrestricted DACL")
        _validate_aces(dacl.value, allowed)
        del token_owner_buffer, buffers
    finally:
        if descriptor.value and _LOCAL_FREE(descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
