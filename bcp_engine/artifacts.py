"""Publicação atômica e validação dos artefatos BCP."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid
from typing import Any, Iterator

from .models import BlockManifest
from .util import (
    file_hash,
    stable_json,
    validate_canonical_uuid,
    validate_sha256_hex,
)


MAX_JSON_ARTIFACT_BYTES = 16 * 1024 * 1024


def read_bounded_json(
    path: Path | str,
    *,
    maximum_bytes: int = MAX_JSON_ARTIFACT_BYTES,
) -> Any:
    """Read a UTF-8 JSON artifact without allowing unbounded allocation."""

    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as exc:
        raise RuntimeError(f"Não foi possível ler o artefato JSON: {candidate.name}") from exc
    if len(payload) > maximum_bytes:
        raise RuntimeError(f"Artefato excede o limite de leitura segura: {candidate.name}")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Artefato JSON inválido: {candidate.name}") from exc


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes where the platform exposes that primitive."""

    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_component(value: str, *, max_length: int = 72) -> str:
    visible = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._") or "item"
    suffix = __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{visible[:max_length]}_{suffix}"


def _contained(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve(strict=False)
    result = candidate.resolve(strict=False)
    try:
        result.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Caminho escapa da raiz de artefatos: {candidate}") from exc
    return result


_CURRENT_WINDOWS_SID: str | None = None


def _current_windows_sid() -> str:
    """Return the current token SID without relying on localized account names."""

    global _CURRENT_WINDOWS_SID
    if _CURRENT_WINDOWS_SID is not None:
        return _CURRENT_WINDOWS_SID
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=True,
            timeout=15,
        )
        rows = list(csv.reader(result.stdout.decode("utf-8", errors="replace").splitlines()))
        sid = rows[0][1].strip() if rows and len(rows[0]) >= 2 else ""
    except (OSError, subprocess.SubprocessError, IndexError, csv.Error) as exc:
        raise RuntimeError(
            "Não foi possível identificar o SID do executor para proteger os artefatos"
        ) from exc
    if not re.fullmatch(r"S-1-[0-9-]+", sid, flags=re.IGNORECASE):
        raise RuntimeError("SID do executor inválido; diretório de artefatos não foi liberado")
    _CURRENT_WINDOWS_SID = sid
    return sid


def _windows_security_descriptor(
    reader_sids: tuple[str, ...], writer_sids: tuple[str, ...]
) -> Any:
    """Build a protected DACL for the executor and explicitly allowed readers."""

    import ctypes
    from ctypes import wintypes

    owner_sid = _current_windows_sid()
    full_control = tuple(
        dict.fromkeys((owner_sid, "S-1-5-18", "S-1-5-32-544", *writer_sids))
    )
    aces = [f"(A;OICI;FA;;;{sid})" for sid in full_control]
    aces.extend(f"(A;OICI;GRGX;;;{sid})" for sid in reader_sids if sid not in full_control)
    sddl = "D:P" + "".join(aces)
    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    convert = ctypes.WinDLL(
        "advapi32", use_last_error=True
    ).ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        raise OSError(ctypes.get_last_error(), "Não foi possível construir a DACL privada")
    return descriptor


def _windows_apply_private_dacl(
    path: Path,
    reader_sids: tuple[str, ...],
    writer_sids: tuple[str, ...],
) -> None:
    """Replace an object's DACL through a no-follow handle.

    A handle-based operation is deliberate: recursive ``icacls`` follows
    junctions and could change an object outside the execution directory.
    """

    import ctypes
    from ctypes import wintypes

    descriptor = _windows_security_descriptor(reader_sids, writer_sids)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    handle: Any = None
    invalid = ctypes.c_void_p(-1).value
    try:
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        )
        get_dacl.restype = wintypes.BOOL
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not get_dacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise OSError(ctypes.get_last_error(), "Não foi possível ler a DACL privada")
        if not present.value or not dacl:
            raise RuntimeError("Descritor de segurança não contém DACL")

        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x00020000 | 0x00040000,  # READ_CONTROL | WRITE_DAC
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,  # OPEN_EXISTING
            0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
            None,
        )
        if handle in (None, invalid):
            raise OSError(
                ctypes.get_last_error(), f"Não foi possível abrir {path.name} para proteger a DACL"
            )

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        information = FileInformation()
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
        get_information.restype = wintypes.BOOL
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(ctypes.get_last_error(), f"Não foi possível inspecionar {path.name}")
        if information.attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise RuntimeError(f"Reparse point não é permitido em artefatos: {path}")
        if not (information.attributes & 0x00000010) and information.number_of_links > 1:
            raise RuntimeError(f"Hard link não é permitido em artefatos: {path}")

        owner = wintypes.LPVOID()
        owner_descriptor = wintypes.LPVOID()
        get_security = advapi32.GetSecurityInfo
        get_security.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        )
        get_security.restype = wintypes.DWORD
        owner_result = get_security(
            handle,
            1,  # SE_FILE_OBJECT
            0x00000001,  # OWNER_SECURITY_INFORMATION
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(owner_descriptor),
        )
        if owner_result != 0 or not owner:
            raise OSError(owner_result, f"Não foi possível validar o owner de {path.name}")
        owner_text = wintypes.LPWSTR()
        try:
            convert_sid = advapi32.ConvertSidToStringSidW
            convert_sid.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
            convert_sid.restype = wintypes.BOOL
            if not convert_sid(owner, ctypes.byref(owner_text)):
                raise OSError(
                    ctypes.get_last_error(), f"Não foi possível ler o SID owner de {path.name}"
                )
            trusted_owners = {
                _current_windows_sid().upper(),
                "S-1-5-18",
                "S-1-5-32-544",
                *(sid.upper() for sid in writer_sids),
            }
            if str(owner_text.value).upper() not in trusted_owners:
                raise RuntimeError(f"Owner não confiável na árvore de artefatos: {path}")
        finally:
            if owner_text:
                kernel32.LocalFree(ctypes.cast(owner_text, wintypes.LPVOID))
            if owner_descriptor:
                kernel32.LocalFree(owner_descriptor)

        set_security = advapi32.SetSecurityInfo
        set_security.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        set_security.restype = wintypes.DWORD
        result = set_security(
            handle,
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL + PROTECTED_DACL
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(result, f"Não foi possível aplicar a DACL privada em {path.name}")
    finally:
        if handle not in (None, invalid):
            kernel32.CloseHandle(handle)
        kernel32.LocalFree(descriptor)


def _windows_tree(path: Path) -> list[Path]:
    """Inventory descendants without following reparse points."""

    pending = [path]
    result: list[Path] = []
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"Não foi possível inventariar artefatos em {directory}") from exc
        for entry in entries:
            candidate = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"Não foi possível inspecionar {candidate}") from exc
            attributes = getattr(info, "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise RuntimeError(f"Reparse point não é permitido em artefatos: {candidate}")
            result.append(candidate)
            if entry.is_dir(follow_symlinks=False):
                pending.append(candidate)
    return result


def _create_posix_private_tree(path: Path) -> None:
    """Create missing path components privately below an already-safe ancestor."""

    missing: list[Path] = []
    cursor = path
    while True:
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            if cursor == cursor.parent:
                raise RuntimeError("Não foi possível localizar ancestral do executor_directory")
            cursor = cursor.parent
            continue
        if not stat.S_ISDIR(info.st_mode) or cursor.is_symlink():
            raise RuntimeError(f"Componente não confiável no caminho de artefatos: {cursor}")
        break

    # Never create below a namespace where another non-root identity may rename
    # the new entry. A sticky directory (for example /tmp) protects entries by
    # owner and is therefore an accepted boundary.
    # An existing requested root is hardened by
    # ``_secure_posix_artifact_root`` immediately afterwards. Its parent is the
    # namespace boundary that must already be safe. For a missing root,
    # ``cursor`` is the first existing parent and must itself be safe.
    ancestor = cursor.parent if cursor == path else cursor
    while True:
        info = ancestor.lstat()
        if (
            info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not info.st_mode & stat.S_ISVTX
        ):
            raise RuntimeError(f"Ancestral gravável e não sticky: {ancestor}")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            # A concurrent creator at a sticky boundary is not trusted. Fail
            # closed instead of adopting an entry whose owner was not proven.
            raise RuntimeError(
                f"Diretório de artefatos apareceu durante a criação: {directory}"
            ) from exc
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _secure_posix_artifact_root(path: Path) -> None:
    """Make the configured root rename-safe without changing its group model."""

    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise RuntimeError("executor_directory deve ser um diretório regular")
        if info.st_uid not in {os.geteuid(), 0}:
            raise RuntimeError("Owner não confiável em executor_directory")
        if info.st_uid == os.geteuid():
            # Keep setgid so a pre-provisioned SQL reader group propagates to
            # every execution and file, while removing rename rights from it.
            path.chmod(info.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
            info = path.lstat()
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            if not (info.st_mode & stat.S_ISVTX):
                raise RuntimeError(
                    "executor_directory permite renomeação por grupo/terceiros"
                )
    except OSError as exc:
        raise RuntimeError("Não foi possível proteger executor_directory") from exc


def _validate_posix_artifact_paths(
    artifact_root: Path,
    execution_root: Path,
    paths: list[Path],
) -> None:
    """Validate the pathname trust boundary used later by SQL Server.

    POSIX locks are inode-scoped while SQL opens a pathname. Therefore every
    directory below the configured root must be owned by this executor and
    deny group/other writes. Same-UID processes and root are the trusted
    boundary; separate mutually untrusted jobs require separate OS identities.
    """

    effective_uid = os.geteuid()
    directories = {artifact_root, execution_root}
    for item in paths:
        parent = item.parent
        while True:
            directories.add(parent)
            if parent == artifact_root:
                break
            try:
                parent.relative_to(artifact_root)
            except ValueError as exc:
                raise RuntimeError("Artefato escapa de executor_directory") from exc
            parent = parent.parent

    for directory in directories:
        try:
            info = directory.lstat()
        except OSError as exc:
            raise RuntimeError(f"Diretório de artefatos indisponível: {directory}") from exc
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise RuntimeError(f"Componente não confiável no caminho de artefatos: {directory}")
        if directory != artifact_root and info.st_uid != effective_uid:
            raise RuntimeError(f"Owner não confiável no caminho de artefatos: {directory}")
        if (
            info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not (directory == artifact_root and info.st_mode & stat.S_ISVTX)
        ):
            raise RuntimeError(f"Diretório de artefatos permite renomeação por terceiros: {directory}")

    # Above executor_directory, a writable sticky directory such as /tmp is
    # safe for an entry owned by this executor; writable non-sticky ancestors
    # would permit pathname substitution.
    ancestor = artifact_root.parent
    while True:
        info = ancestor.lstat()
        if (
            info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not info.st_mode & stat.S_ISVTX
        ):
            raise RuntimeError(f"Ancestral gravável e não sticky: {ancestor}")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent


def _secure_execution_directory(
    path: Path,
    reader_sids: tuple[str, ...],
    writer_sids: tuple[str, ...],
) -> None:
    """Remove broad Windows inheritance or POSIX write access from an execution."""

    if os.name == "nt":
        try:
            # Protect the root before walking its children. No recursive path
            # operation is used, so junctions can never redirect an ACL write.
            _windows_apply_private_dacl(path, reader_sids, writer_sids)
            before = _windows_tree(path)
            for descendant in before:
                _windows_apply_private_dacl(descendant, reader_sids, writer_sids)
            after = _windows_tree(path)
            if {str(item) for item in before} != {str(item) for item in after}:
                raise RuntimeError("A árvore de artefatos mudou durante a proteção da DACL")
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "Não foi possível aplicar a DACL privada no diretório da execução"
            ) from exc
        return
    try:
        inherited_setgid = path.lstat().st_mode & stat.S_ISGID
        path.chmod(0o750 | inherited_setgid)
    except OSError as exc:
        raise RuntimeError(
            "Não foi possível remover escrita pública do diretório da execução"
        ) from exc


def _harden_published_file(path: Path) -> None:
    if os.name == "posix":
        descriptor = -1
        try:
            descriptor = _open_posix_regular(path)
            os.fchmod(descriptor, 0o440)
            os.fsync(descriptor)
        except OSError as exc:
            raise RuntimeError(f"Não foi possível proteger o artefato publicado: {path.name}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _open_posix_regular(path: Path, *, writable: bool = False) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            raise RuntimeError(f"Artefato não é arquivo regular estável: {path.name}")
        if opened.st_uid != os.geteuid():
            raise RuntimeError(f"Owner não confiável no artefato: {path.name}")
        if opened.st_nlink != 1:
            raise RuntimeError(f"Hard link não é permitido em artefatos: {path.name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_descriptor_path_identity(descriptor: int, path: Path) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = path.lstat()
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError(f"Artefato foi substituído durante a publicação: {path.name}")
    return opened


def _descriptor_sha256(descriptor: int) -> str:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


class _ArtifactReadLease:
    """Hold read leases that prevent mutation throughout hash check and bulk read."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self._resources: list[tuple[str, int]] = []
        self._descriptors: dict[str, int] = {}

    def __enter__(self) -> "_ArtifactReadLease":
        try:
            for path in self.paths:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"Artefato não é arquivo regular: {path.name}")
                if os.name == "nt":
                    descriptor = self._open_windows(path)
                else:
                    descriptor = self._open_posix(path)
                key = self._key(path)
                self._resources.append((key, descriptor))
                self._descriptors[key] = descriptor
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def _open_windows(path: Path) -> int:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ: deny concurrent write/delete opens
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            raise OSError(error, f"Não foi possível obter lease somente leitura: {path.name}")
        try:
            class FileInformation(ctypes.Structure):
                _fields_ = [
                    ("attributes", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME),
                    ("last_access_time", wintypes.FILETIME),
                    ("last_write_time", wintypes.FILETIME),
                    ("volume_serial_number", wintypes.DWORD),
                    ("file_size_high", wintypes.DWORD),
                    ("file_size_low", wintypes.DWORD),
                    ("number_of_links", wintypes.DWORD),
                    ("file_index_high", wintypes.DWORD),
                    ("file_index_low", wintypes.DWORD),
                ]

            information = FileInformation()
            get_information = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GetFileInformationByHandle
            get_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
            get_information.restype = wintypes.BOOL
            if not get_information(handle, ctypes.byref(information)):
                raise OSError(ctypes.get_last_error(), f"Não foi possível inspecionar {path.name}")
            if information.attributes & (0x00000400 | 0x00000010):
                raise RuntimeError(f"Artefato não é arquivo regular: {path.name}")
            if information.number_of_links > 1:
                raise RuntimeError(f"Hard link não é permitido em artefatos: {path.name}")
            return msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise

    @staticmethod
    def _open_posix(path: Path) -> Any:
        import fcntl

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            current = path.stat()
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (current.st_dev, current.st_ino):
                raise RuntimeError(f"Artefato foi substituído durante a proteção: {path.name}")
            if opened.st_uid != os.geteuid():
                raise RuntimeError(f"Owner não confiável no artefato: {path.name}")
            if opened.st_nlink != 1:
                raise RuntimeError(f"Hard link não é permitido em artefatos: {path.name}")
            if opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError(
                    f"Artefato permite escrita por grupo/terceiros: {path.name}"
                )
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def read_bytes(self, path: Path, *, maximum_bytes: int | None = None) -> bytes:
        descriptor = self._descriptors.get(self._key(path))
        if descriptor is None:
            raise RuntimeError(f"Artefato não pertence ao lease: {path.name}")
        size = int(os.fstat(descriptor).st_size)
        if maximum_bytes is not None and size > maximum_bytes:
            raise RuntimeError(f"Artefato excede o limite de leitura segura: {path.name}")
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if maximum_bytes is not None and total > maximum_bytes:
                    raise RuntimeError(f"Artefato excede o limite de leitura segura: {path.name}")
                chunks.append(chunk)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)

    def sha256_hex(self, path: Path) -> str:
        descriptor = self._descriptors.get(self._key(path))
        if descriptor is None:
            raise RuntimeError(f"Artefato não pertence ao lease: {path.name}")
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        digest = hashlib.sha256()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)

    def size(self, path: Path) -> int:
        descriptor = self._descriptors.get(self._key(path))
        if descriptor is None:
            raise RuntimeError(f"Artefato não pertence ao lease: {path.name}")
        return int(os.fstat(descriptor).st_size)

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        while self._resources:
            key, descriptor = self._resources.pop()
            self._descriptors.pop(key, None)
            os.close(descriptor)


@dataclass(frozen=True)
class BlockPaths:
    directory: Path
    data: Path
    partial: Path
    format: Path
    manifest: Path
    log: Path
    error: Path


@dataclass(frozen=True)
class VerifiedArtifacts:
    """Canonical paths kept under read leases for the complete SQL operation."""

    manifest: BlockManifest
    manifest_path: Path
    format_path: Path
    data_path: Path | None


class ArtifactStore:
    def __init__(
        self,
        root: Path | str,
        execution_id: str,
        *,
        reader_sids: tuple[str, ...] | list[str] = (),
        writer_sids: tuple[str, ...] | list[str] = (),
    ) -> None:
        validate_canonical_uuid(execution_id, "execution_id")
        self.root = Path(root)
        if not self.root.is_absolute():
            # Em Windows rodando testes no POSIX, Path não entende drive; o
            # validador de configuração trata a sintaxe Windows. Em runtime
            # Windows, esta checagem sempre é verdadeira para drive/UNC.
            raise ValueError("executor_directory deve ser absoluto")
        self.reader_sids = tuple(reader_sids)
        self.writer_sids = tuple(writer_sids)
        if os.name == "posix":
            _create_posix_private_tree(self.root)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        if os.name == "posix":
            _secure_posix_artifact_root(self.root)
            _fsync_directory(self.root)
        else:
            # executor_directory is a security boundary: protecting only its
            # UUID child would still let a principal with DELETE_CHILD replace
            # that child by name.
            _windows_apply_private_dacl(
                self.root, self.reader_sids, self.writer_sids
            )
        self.execution_id = execution_id
        self.execution_root = _contained(self.root, self.root / execution_id)
        execution_existed = self.execution_root.exists()
        self.execution_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _secure_execution_directory(
            self.execution_root, self.reader_sids, self.writer_sids
        )
        if os.name == "posix":
            _fsync_directory(self.execution_root)
            if not execution_existed:
                _fsync_directory(self.execution_root.parent)
            _validate_posix_artifact_paths(self.root, self.execution_root, [])

    def table_directory(self, table_id: str) -> Path:
        validate_sha256_hex(table_id, "table_id")
        # Keep enough headroom for the block directory, manifest suffix and
        # temporary publication suffix on Windows hosts that still enforce
        # MAX_PATH. The directory remains deterministic and carries 176 bits
        # of the SHA-256 identity (32 visible + 12 suffix hex digits).
        result = _contained(
            self.execution_root,
            self.execution_root / _safe_component(table_id, max_length=32),
        )
        table_existed = result.exists()
        result.mkdir(mode=0o750, parents=True, exist_ok=True)
        if os.name == "posix":
            result.chmod(0o750 | (result.lstat().st_mode & stat.S_ISGID))
            _fsync_directory(result)
            if not table_existed:
                _fsync_directory(result.parent)
        return result

    def block_paths(self, table_id: str, block_number: int, block_id: str) -> BlockPaths:
        validate_canonical_uuid(block_id, "block_id")
        if block_number < 1:
            raise ValueError("Número de bloco inválido")
        directory = self.table_directory(table_id) / f"block_{block_number:012d}_{block_id}"
        directory = _contained(self.execution_root, directory)
        stem = f"block_{block_number:012d}"
        block_existed = directory.exists()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = BlockPaths(
            directory=directory,
            data=directory / f"{stem}.bcp",
            partial=directory / f"{stem}.bcp.partial",
            format=directory / f"{stem}.xml",
            manifest=directory / f"{stem}.manifest.json",
            log=directory / f"{stem}.bcp.log",
            error=directory / f"{stem}.bcp.error",
        )
        if os.name == "posix":
            inherited_setgid = directory.lstat().st_mode & stat.S_ISGID
            # Before publication the directory is executor-only, so BCP and
            # Python outputs cannot be opened RW by a shared SQL reader group.
            # Existing artifacts are revalidated before this directory becomes
            # traversable again; mere presence of a manifest is not proof.
            directory.chmod(0o700 | inherited_setgid)
            _fsync_directory(directory)
            if not block_existed:
                _fsync_directory(directory.parent)
        return result

    def probe(self, *, require_delete: bool) -> None:
        probe_dir = self.execution_root / ".probe"
        probe_dir.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            probe_dir.chmod(0o700)
        token = os.urandom(32)
        source = _contained(self.execution_root, probe_dir / (uuid.uuid4().hex + ".partial"))
        target = source.with_suffix(".final")
        try:
            with source.open("xb") as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            if source.read_bytes() != token:
                raise RuntimeError("Falha na prova de leitura do diretório executor")
            os.replace(source, target)
            if target.read_bytes() != token:
                raise RuntimeError("Falha na prova de renomeação atômica")
            if require_delete:
                target.unlink()
                if target.exists():
                    raise RuntimeError("Falha na prova de exclusão")
        finally:
            source.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            try:
                probe_dir.rmdir()
            except OSError:
                pass

    @staticmethod
    def disk_free(path: Path) -> int | None:
        try:
            return int(shutil.disk_usage(path).free)
        except OSError:
            return None

    def assert_capacity(self, path: Path, minimum_free: int, estimated_next: int = 0) -> None:
        free = self.disk_free(path)
        if free is None:
            raise RuntimeError("Espaço livre indisponível; política segura interrompe a execução")
        if free - max(0, estimated_next) < minimum_free:
            raise RuntimeError(
                f"Espaço insuficiente: livre={free}, próximo_bloco_estimado={estimated_next}, "
                f"reserva_mínima={minimum_free}"
            )

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        partial = path.with_suffix(path.suffix + ".partial")
        payload = (stable_json(value) + "\n").encode("utf-8")
        # A partial is never evidence of a completed publication. It can remain
        # after a crash between fsync and replace, so a retry may supersede it.
        partial.unlink(missing_ok=True)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(partial, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
        _fsync_directory(path.parent)

    @staticmethod
    def _assert_same_block_identity(existing: BlockManifest, requested: BlockManifest) -> None:
        """Reject a valid manifest that belongs to another logical block."""
        identity_fields = (
            "manifest_version",
            "execution_id",
            "dataset_id",
            "table_id",
            "block_id",
            "block_number",
            "source",
            "destination",
            "authentication",
            "profile",
            "projection",
            "projection_hash",
            "layout_hash",
            "watermark",
            "lower_bound",
            "upper_bound",
            "final_limit",
            "consistency",
            "empty_range",
            "metadata_mapping",
            "layout_contract",
            "table_empty",
            "transfer_mode",
            "source_rows_at_capture",
        )
        different = []
        for field in identity_fields:
            left = getattr(existing, field)
            right = getattr(requested, field)
            # JSON nao preserva tuplas. Compare o snapshot estrutural pela
            # representacao canonica para manter a republicacao idempotente.
            equal = (
                stable_json(left) == stable_json(right)
                if field == "layout_contract"
                else left == right
            )
            if not equal:
                different.append(field)
        if different:
            raise RuntimeError(
                "Manifesto final existente pertence a outra identidade de bloco "
                "ou estrutura: " + ", ".join(different)
            )

    def publish(self, paths: BlockPaths, manifest: BlockManifest) -> BlockManifest:
        for candidate in (
            paths.directory,
            paths.data,
            paths.partial,
            paths.format,
            paths.manifest,
        ):
            _contained(self.execution_root, candidate)
        if os.name == "posix":
            _validate_posix_artifact_paths(
                self.root,
                self.execution_root,
                [
                    paths.directory,
                    paths.data,
                    paths.partial,
                    paths.format,
                    paths.manifest,
                ],
            )
        if paths.manifest.exists():
            existing = self.load_and_verify(paths.manifest)
            self._assert_same_block_identity(existing, manifest)
            if os.name == "posix":
                paths.directory.chmod(
                    0o750 | (paths.directory.lstat().st_mode & stat.S_ISGID)
                )
                _fsync_directory(paths.directory)
            return existing
        if manifest.empty_range:
            if paths.partial.exists() or paths.data.exists():
                raise RuntimeError("Faixa vazia não pode publicar arquivo de dados")
            manifest.data_file = None
            manifest.data_sha256 = None
            manifest.file_bytes = 0
        else:
            if not paths.partial.is_file():
                raise RuntimeError("Arquivo parcial ausente; cursor não será avançado")
            if os.name == "posix":
                descriptor = _open_posix_regular(paths.partial, writable=True)
                try:
                    # Remove write permission before hashing/publication. The
                    # already-open descriptor remains usable for fsync/hash.
                    os.fchmod(descriptor, 0o440)
                    os.fsync(descriptor)
                    _assert_descriptor_path_identity(descriptor, paths.partial)
                    os.replace(paths.partial, paths.data)
                    _fsync_directory(paths.directory)
                    opened = _assert_descriptor_path_identity(descriptor, paths.data)
                    manifest.data_file = paths.data.name
                    manifest.file_bytes = int(opened.st_size)
                    manifest.data_sha256 = _descriptor_sha256(descriptor)
                finally:
                    os.close(descriptor)
            else:
                # On Windows, fsync needs a descriptor opened for update. The
                # execution DACL and unique block directory prevent substitution.
                with paths.partial.open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(paths.partial, paths.data)
                _fsync_directory(paths.directory)
                manifest.data_file = paths.data.name
                manifest.file_bytes = paths.data.stat().st_size
                manifest.data_sha256 = file_hash(paths.data)
                _harden_published_file(paths.data)
        if not paths.format.is_file():
            raise RuntimeError("Format file ausente")
        manifest.format_file = paths.format.name
        if os.name == "posix":
            descriptor = _open_posix_regular(paths.format)
            try:
                os.fchmod(descriptor, 0o440)
                os.fsync(descriptor)
                _assert_descriptor_path_identity(descriptor, paths.format)
                manifest.format_sha256 = _descriptor_sha256(descriptor)
            finally:
                os.close(descriptor)
        else:
            with paths.format.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            manifest.format_sha256 = file_hash(paths.format)
            _harden_published_file(paths.format)
        _fsync_directory(paths.directory)
        manifest.complete = True
        manifest.validate()
        self._atomic_json(paths.manifest, manifest.as_dict())
        _harden_published_file(paths.manifest)
        if os.name == "posix":
            paths.directory.chmod(
                0o750 | (paths.directory.lstat().st_mode & stat.S_ISGID)
            )
            _fsync_directory(paths.directory)
        # Reabre o manifesto final para não confiar apenas no objeto em memória.
        return self.load_and_verify(paths.manifest)

    def load_and_verify(self, manifest_path: Path | str) -> BlockManifest:
        return self._load_and_verify(manifest_path, allow_missing_data=False)

    def load_for_reconciliation(self, manifest_path: Path | str) -> BlockManifest:
        """Valida manifesto/formato e qualquer dado presente.

        O unico relaxamento e aceitar que o ``.bcp`` tenha sido removido pela
        politica pos-commit. Isso permite consultar o controle SQL e reconhecer
        idempotentemente um bloco ja confirmado, sem aceitar arquivo alterado.
        """

        return self._load_and_verify(manifest_path, allow_missing_data=True)

    def retained_data_exists(self, manifest_path: Path | str) -> bool:
        """Return whether a completed manifest still has its local data file.

        Reporting only needs an existence count.  Re-hashing every retained
        multi-gigabyte block during a no-op resume would make that report as
        expensive as the import preflight, so this method validates the
        manifest contract and path containment without reading the data bytes.
        Integrity is still checked by ``load_and_verify`` before every import.
        """

        path = _contained(self.execution_root, Path(manifest_path))
        if path.name.endswith(".partial") or not path.is_file():
            return False
        manifest = BlockManifest.from_dict(read_bounded_json(path))
        if manifest.empty_range or not manifest.data_file:
            return False
        data = _contained(path.parent, path.parent / manifest.data_file)
        return data.is_file()

    def _load_and_verify(
        self,
        manifest_path: Path | str,
        *,
        allow_missing_data: bool,
    ) -> BlockManifest:
        path = _contained(self.execution_root, Path(manifest_path))
        if path.name.endswith(".partial") or not path.is_file():
            raise RuntimeError("Manifesto final ausente ou parcial")
        value = read_bounded_json(path)
        manifest = BlockManifest.from_dict(value)
        base = path.parent
        fmt = _contained(base, base / manifest.format_file)
        if not fmt.is_file() or file_hash(fmt) != manifest.format_sha256:
            raise RuntimeError("Hash do format file diverge do manifesto")
        if not manifest.empty_range:
            assert manifest.data_file and manifest.data_sha256
            data = _contained(base, base / manifest.data_file)
            if not data.is_file():
                if allow_missing_data:
                    return manifest
                raise RuntimeError("Arquivo de dados final ausente")
            if data.name.endswith(".partial"):
                raise RuntimeError("Arquivo de dados final ausente")
            if data.stat().st_size != manifest.file_bytes or file_hash(data) != manifest.data_sha256:
                raise RuntimeError("Arquivo de dados foi modificado após publicação")
        if os.name == "posix":
            protected = [path, fmt]
            if not manifest.empty_range and data.is_file():
                protected.append(data)
            for candidate in protected:
                _harden_published_file(candidate)
            base.chmod(0o750 | (base.lstat().st_mode & stat.S_ISGID))
            _fsync_directory(base)
        return manifest

    @contextmanager
    def hold_verified_artifacts(
        self,
        manifest_path: Path | str,
    ) -> Iterator[VerifiedArtifacts]:
        """Verify again while mutation is blocked until the caller finishes SQL."""

        manifest_file = _contained(self.execution_root, Path(manifest_path))
        if manifest_file.name.endswith(".partial"):
            raise RuntimeError("Manifesto final ausente ou parcial")
        if os.name == "posix":
            _validate_posix_artifact_paths(
                self.root, self.execution_root, [manifest_file]
            )
        # Stabilize the manifest first and derive every dependent filename from
        # bytes read through that already-open handle. This closes the gap where
        # a valid empty manifest could be exchanged for a non-empty one before
        # the data-file lease was acquired.
        with _ArtifactReadLease([manifest_file]) as manifest_lease:
            try:
                value = json.loads(
                    manifest_lease.read_bytes(
                        manifest_file, maximum_bytes=MAX_JSON_ARTIFACT_BYTES
                    ).decode("utf-8")
                )
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Manifesto final inválido") from exc
            manifest = BlockManifest.from_dict(value)
            base = manifest_file.parent
            fmt = _contained(base, base / manifest.format_file)
            protected = [fmt]
            data: Path | None = None
            if not manifest.empty_range:
                assert manifest.data_file and manifest.data_sha256
                data = _contained(base, base / manifest.data_file)
                protected.append(data)
            if os.name == "posix":
                _validate_posix_artifact_paths(
                    self.root,
                    self.execution_root,
                    [manifest_file, *protected],
                )
            with _ArtifactReadLease(protected) as payload_lease:
                if payload_lease.sha256_hex(fmt) != manifest.format_sha256:
                    raise RuntimeError("Hash do format file diverge do manifesto")
                if data is not None:
                    if (
                        payload_lease.size(data) != manifest.file_bytes
                        or payload_lease.sha256_hex(data) != manifest.data_sha256
                    ):
                        raise RuntimeError("Arquivo de dados foi modificado após publicação")
                if os.name == "posix":
                    for candidate in [manifest_file, fmt, *([data] if data else [])]:
                        _harden_published_file(candidate)
                    base.chmod(0o750 | (base.lstat().st_mode & stat.S_ISGID))
                    _fsync_directory(base)
                yield VerifiedArtifacts(
                    manifest=manifest,
                    manifest_path=manifest_file,
                    format_path=fmt,
                    data_path=data,
                )

    def remove_confirmed_data(self, manifest_path: Path | str) -> None:
        # O controle SQL pode confirmar o bloco e o processo cair logo depois
        # de remover o arquivo. Uma nova reconciliacao deve repetir esta etapa
        # sem transformar um pos-commit concluido em erro.
        manifest_file = _contained(self.execution_root, Path(manifest_path))
        manifest = self.load_for_reconciliation(manifest_file)
        if manifest.empty_range or not manifest.data_file:
            return
        path = manifest_file.parent / manifest.data_file
        _contained(self.execution_root, path).unlink(missing_ok=True)
