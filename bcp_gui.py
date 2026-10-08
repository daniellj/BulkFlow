#!/usr/bin/env python3
"""Entry point for the optional ttkbootstrap desktop adapter."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interface gráfica do BulkFlow - SQL Server Data Export & Load"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Arquivo JSON V2 para abrir ao iniciar (opcional)",
    )
    args = parser.parse_args()
    try:
        from bcp_engine.gui import main as gui_main
    except ModuleNotFoundError as error:
        if error.name == "ttkbootstrap":
            raise SystemExit(
                "A interface requer ttkbootstrap. Execute: python -m pip install -r requirements.txt"
            ) from None
        raise
    return gui_main(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
