"""Windows DPAPI helpers for short-lived, non-interactive execution packages."""

from __future__ import annotations

import base64
import os

from .auth import AuthError


def _crypt(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise AuthError("Segredos DPAPI só estão disponíveis no executor Windows")
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        def blob(value: bytes) -> tuple[DATA_BLOB, ctypes.Array[ctypes.c_char]]:
            buffer = ctypes.create_string_buffer(value)
            return (
                DATA_BLOB(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
                buffer,
            )

        source, source_buffer = blob(data)
        output = DATA_BLOB()
        crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            wintypes.LPCWSTR if protect else ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        function.restype = wintypes.BOOL
        flags = 0x4 if protect else 0  # CRYPTPROTECT_LOCAL_MACHINE
        description = "BulkFlow temporary execution package" if protect else None
        if not function(
            ctypes.byref(source),
            description,
            None,
            None,
            None,
            flags,
            ctypes.byref(output),
        ):
            raise AuthError(f"Falha DPAPI do Windows (erro Win32 {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
            del source_buffer
    except AuthError:
        raise
    except Exception as error:
        raise AuthError(f"Falha ao processar segredo DPAPI: {error}") from None


def protect_machine_secret(value: str) -> str:
    """Encrypt UTF-8 text for non-interactive processes on this Windows host."""

    encrypted = _crypt(value.encode("utf-8"), protect=True)
    return base64.b64encode(encrypted).decode("ascii")


def unprotect_machine_secret(value: str) -> str:
    """Decrypt a machine-scoped DPAPI payload."""

    try:
        encrypted = base64.b64decode(value, validate=True)
    except Exception as error:
        raise AuthError("Payload DPAPI inválido") from error
    try:
        return _crypt(encrypted, protect=False).decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuthError("Payload DPAPI não contém texto UTF-8 válido") from error
