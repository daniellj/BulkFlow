"""Read-only verification of the native tools required at execution time."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .bcp import BcpRunner
from .runtime import resolve_bcp_executable
from .util import redacted_exception


@dataclass(frozen=True)
class PrerequisiteReport:
    odbc_driver: str
    odbc_driver_available: bool
    installed_odbc_drivers: tuple[str, ...]
    bcp_executable: str
    bcp_resolved_path: str | None
    bcp_version: str | None
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.odbc_driver_available and self.bcp_version is not None and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ready": self.ready}


def inspect_prerequisites(config: Mapping[str, Any]) -> PrerequisiteReport:
    """Inspect Driver Manager registration and BCP without changing the host."""

    expected_driver = str(config.get("odbc_driver", "ODBC Driver 18 for SQL Server"))
    executable = str(config.get("bcp_executable", "bcp"))
    errors: list[str] = []
    installed: tuple[str, ...] = ()
    try:
        import pyodbc

        installed = tuple(str(item) for item in pyodbc.drivers())
    except Exception as exc:
        errors.append("ODBC_DRIVER_MANAGER_UNAVAILABLE: " + redacted_exception(exc))
    driver_available = expected_driver.casefold() in {
        item.casefold() for item in installed
    }
    if not driver_available:
        errors.append("ODBC_DRIVER_NOT_INSTALLED: " + expected_driver)

    resolved = resolve_bcp_executable(executable)
    version: str | None = None
    if resolved is None:
        errors.append("BCP_NOT_FOUND: " + executable)
    else:
        try:
            version = BcpRunner.require_version(resolved)
        except Exception as exc:
            errors.append("BCP_VERSION_INVALID: " + redacted_exception(exc))

    return PrerequisiteReport(
        odbc_driver=expected_driver,
        odbc_driver_available=driver_available,
        installed_odbc_drivers=installed,
        bcp_executable=executable,
        bcp_resolved_path=resolved,
        bcp_version=version,
        errors=tuple(errors),
    )


__all__ = ["PrerequisiteReport", "inspect_prerequisites"]
