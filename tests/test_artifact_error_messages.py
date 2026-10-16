from pathlib import Path

from bcp_engine.artifacts import _windows_dacl_open_error_message


def test_windows_dacl_error_identifies_full_path_and_both_security_contexts():
    path = Path(r"\\servidor\compartilhamento\bd_export_import")

    message = _windows_dacl_open_error_message(path)

    assert str(path) in message
    assert "conta que executa o BulkFlow" in message
    assert "conta do serviço SQL Server" in message
    assert "ainda não foi testada" in message
