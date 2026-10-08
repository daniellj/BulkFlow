"""Runtime path discovery shared by installed and source distributions.

The installed Windows application keeps immutable binaries under Program Files
and mutable operator data under LocalAppData. BCP discovery also avoids
depending on a stale process PATH after the offline installer has provisioned
the Microsoft command-line utilities.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import shutil
import sys


PRODUCT_DATA_DIRECTORY = "BulkFlow"
SOURCE_LOCAL_DIRECTORY = "Local"
CONTROL_DIRECTORY_NAME = ".bcp-control"
EXPORT_DIRECTORY_NAME = "bcp-data"
DDL_DIRECTORY_NAME = "ddl"
INSTALLER_REGISTRY_KEY = r"SOFTWARE\BulkFlow"
INSTALLER_BCP_VALUE = "BcpExecutable"


def is_frozen_application() -> bool:
    """Return whether the current process is a frozen application bundle."""

    return bool(getattr(sys, "frozen", False))


def _frozen_source_project_root() -> Path | None:
    """Return the source-tree root when a frozen binary runs from ``release``.

    Release executables kept inside the project are portable homologation
    artifacts and must retain the same ``<project>/Local/BulkFlow`` defaults as
    source execution. Installed/copy-only binaries do not have the source-tree
    markers and continue to use the writable per-user LocalAppData directory.
    """

    if not is_frozen_application():
        return None
    try:
        executable_directory = Path(sys.executable).resolve().parent
    except OSError:
        return None
    candidates = (executable_directory, executable_directory.parent)
    for candidate in candidates:
        if (
            (candidate / "bcp_bronze.py").is_file()
            and (candidate / "templates").is_dir()
            and (candidate / "schemas").is_dir()
        ):
            return candidate
    return None


def runtime_data_root(
    project_root: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the writable root used by runtime-generated application data.

    An explicitly supplied root is primarily useful to callers and tests that
    operate from the source tree. Unfrozen execution keeps generated data below
    ``<project root>/Local/BulkFlow``. A frozen release executed from inside a
    source checkout keeps that project-local convention. An installed or
    standalone frozen Windows build uses LocalAppData so an installation under
    Program Files never needs write permission there.
    """

    if project_root is not None:
        return (
            Path(project_root).resolve()
            / SOURCE_LOCAL_DIRECTORY
            / PRODUCT_DATA_DIRECTORY
        )
    if not is_frozen_application():
        return (
            Path(__file__).resolve().parents[1]
            / SOURCE_LOCAL_DIRECTORY
            / PRODUCT_DATA_DIRECTORY
        )

    frozen_project_root = _frozen_source_project_root()
    if frozen_project_root is not None:
        return (
            frozen_project_root
            / SOURCE_LOCAL_DIRECTORY
            / PRODUCT_DATA_DIRECTORY
        ).resolve()

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data).expanduser()
    elif os.name == "nt":
        # LOCALAPPDATA is normally present in an interactive Windows session.
        # The deterministic fallback also supports restricted service accounts.
        base = Path.home() / "AppData" / "Local"
    else:
        # Frozen non-Windows builds are not currently shipped, but retaining an
        # XDG-style fallback keeps this helper platform-neutral.
        base = Path(values.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / PRODUCT_DATA_DIRECTORY).resolve()


def runtime_control_directory(project_root: Path | None = None) -> Path:
    """Return the default local SQLite/checkpoint directory."""

    return runtime_data_root(project_root) / CONTROL_DIRECTORY_NAME


def runtime_export_directory(project_root: Path | None = None) -> Path:
    """Return the default directory for BCP data artifacts."""

    return runtime_data_root(project_root) / EXPORT_DIRECTORY_NAME


def runtime_ddl_directory(project_root: Path | None = None) -> Path:
    """Return the default directory for generated DDL scripts."""

    return runtime_data_root(project_root) / DDL_DIRECTORY_NAME


def ensure_runtime_directories(
    project_root: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Create and return the default control, export, and DDL directories."""

    directories = (
        runtime_control_directory(project_root),
        runtime_export_directory(project_root),
        runtime_ddl_directory(project_root),
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return directories


def runtime_config_directory(project_root: Path | None = None) -> Path:
    """Return the default directory for GUI configuration files."""

    if project_root is not None:
        return Path(project_root).resolve()
    if not is_frozen_application():
        return Path(__file__).resolve().parents[1]
    return runtime_data_root() / "config"


def _is_windows() -> bool:
    return os.name == "nt"


def _existing_file(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    candidate = Path(os.path.expandvars(text)).expanduser()
    try:
        if candidate.is_file():
            return str(candidate.resolve())
    except OSError:
        return None
    return None


def _read_installer_bcp_path() -> str | None:
    """Read the BCP path recorded by the offline installer, when available."""

    if not _is_windows():
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - defensive for unusual runtimes
        return None

    views = [
        getattr(winreg, "KEY_WOW64_64KEY", 0),
        getattr(winreg, "KEY_WOW64_32KEY", 0),
    ]
    seen: set[int] = set()
    for view in views:
        if view in seen:
            continue
        seen.add(view)
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                INSTALLER_REGISTRY_KEY,
                0,
                winreg.KEY_READ | view,
            ) as key:
                value, _value_type = winreg.QueryValueEx(key, INSTALLER_BCP_VALUE)
        except OSError:
            continue
        resolved = _existing_file(value)
        if resolved is not None:
            return resolved
    return None


def _program_files_roots(environment: Mapping[str, str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()
    for variable in (
        "ProgramW6432",
        "ProgramFiles",
        "PROGRAMFILES",
        "ProgramFiles(x86)",
    ):
        value = environment.get(variable)
        if not value:
            continue
        candidate = Path(value).expanduser()
        normalized = str(candidate).casefold()
        if normalized not in seen:
            seen.add(normalized)
            roots.append(candidate)
    return tuple(roots)


def _installed_bcp_candidates(environment: Mapping[str, str]) -> tuple[Path, ...]:
    """Return official SQL Client SDK paths in deterministic preference order."""

    candidates: list[Path] = []
    for program_files in _program_files_roots(environment):
        odbc_root = program_files / "Microsoft SQL Server" / "Client SDK" / "ODBC"
        candidates.append(odbc_root / "180" / "Tools" / "Binn" / "bcp.exe")
        try:
            numbered = sorted(
                (
                    child
                    for child in odbc_root.iterdir()
                    if child.is_dir()
                    and child.name.isdecimal()
                    and child.name != "180"
                ),
                key=lambda child: int(child.name),
                reverse=True,
            )
        except OSError:
            numbered = []
        candidates.extend(
            child / "Tools" / "Binn" / "bcp.exe" for child in numbered
        )
    return tuple(candidates)


def resolve_bcp_executable(
    executable: str | os.PathLike[str] = "bcp",
    *,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve BCP without allowing PATH to override an installed trusted path.

    Resolution order is: an explicit operator path; the installer registry
    value on Windows; the official ODBC 180 Client SDK location followed by
    other numeric versions; and finally PATH. A missing explicit path is not
    silently replaced by another executable.
    """

    requested = str(executable).strip().strip('"')
    if not requested:
        requested = "bcp"
    candidate = Path(requested).expanduser()
    has_directory = (
        candidate.is_absolute()
        or candidate.parent != Path(".")
        or "/" in requested
        or "\\" in requested
    )
    if has_directory:
        return _existing_file(requested)

    values = os.environ if environment is None else environment
    generic_bcp = candidate.name.casefold() in {"bcp", "bcp.exe"}
    if _is_windows() and generic_bcp:
        registered = _read_installer_bcp_path()
        if registered is not None:
            return registered
        for installed in _installed_bcp_candidates(values):
            resolved = _existing_file(installed)
            if resolved is not None:
                return resolved

    # shutil.which returns a qualified path and does not perform shell
    # expansion. Passing the explicit environment keeps tests and embedded
    # execution deterministic.
    search_path = values.get("PATH") if environment is None else values.get("PATH", "")
    resolved = shutil.which(requested, path=search_path)
    return _existing_file(resolved)


__all__ = [
    "CONTROL_DIRECTORY_NAME",
    "DDL_DIRECTORY_NAME",
    "EXPORT_DIRECTORY_NAME",
    "INSTALLER_BCP_VALUE",
    "INSTALLER_REGISTRY_KEY",
    "PRODUCT_DATA_DIRECTORY",
    "SOURCE_LOCAL_DIRECTORY",
    "ensure_runtime_directories",
    "is_frozen_application",
    "resolve_bcp_executable",
    "runtime_config_directory",
    "runtime_control_directory",
    "runtime_data_root",
    "runtime_ddl_directory",
    "runtime_export_directory",
]
