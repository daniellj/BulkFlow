import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"
SQLSERVER = DOCKER / "sqlserver"


@unittest.skipUnless(DOCKER.is_dir() and SQLSERVER.is_dir(), "laboratorio Docker local ausente")
class DockerControlDboTests(unittest.TestCase):
    def test_all_lab_configs_use_canonical_dbo_control(self):
        configs = sorted(DOCKER.glob("config.integration*.json"))
        self.assertTrue(configs)
        for path in configs:
            with self.subTest(path=path.name):
                config = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(config["control_schema"], "dbo")

    def test_admin_migration_drops_legacy_child_first_and_bootstraps_dbo(self):
        sql = (SQLSERVER / "migrate-control-to-dbo.sql").read_text(encoding="utf-8")
        drop_order = [
            "DROP TABLE [controle_transferencia].[execucao_lote]",
            "DROP TABLE [controle_transferencia].[execucao_tabela]",
            "DROP TABLE [controle_transferencia].[execucao]",
            "DROP TABLE [controle_transferencia].[versao_esquema]",
            "DROP SCHEMA [controle_transferencia]",
        ]
        offsets = [sql.index(statement) for statement in drop_order]
        self.assertEqual(offsets, sorted(offsets))
        for object_name in ("ctl_exec_versao", "ctl_exec", "ctl_exec_tabela", "ctl_exec_lote"):
            self.assertIn(f"CREATE TABLE [dbo].[{object_name}]", sql)
        self.assertIn("DENY ALTER ON SCHEMA::[dbo] TO [u684]", sql)
        self.assertIn("GRANT SELECT ON OBJECT::[dbo].[ctl_exec_versao]", sql)
        self.assertIn("GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES", sql)
        self.assertIn("EXEC sys.sp_rename N'dbo.execucao', N'ctl_exec'", sql)
        self.assertIn("UPDATE [dbo].[ctl_exec_versao] SET [version] = 3", sql)

    def test_fixture_bootstraps_control_as_admin_and_keeps_dbo_denied(self):
        init_target = (SQLSERVER / "init-target.sql").read_text(encoding="utf-8")
        init_shell = (SQLSERVER / "init.sh").read_text(encoding="utf-8")
        self.assertNotIn("CREATE SCHEMA [controle_transferencia]", init_target)
        self.assertIn("DENY ALTER ON SCHEMA::[dbo] TO [u684]", init_target)
        bootstrap = "migrate-control-to-dbo.sql"
        self.assertIn(bootstrap, init_shell)
        self.assertLess(init_shell.index("init-target.sql"), init_shell.index(bootstrap))
        self.assertLess(init_shell.index(bootstrap), init_shell.index("normalize-target-identifiers.sql"))

    def test_reset_and_verification_target_dbo_and_reject_legacy_schema(self):
        reset = (SQLSERVER / "reset-target-data.sql").read_text(encoding="utf-8")
        verify = (SQLSERVER / "verify-target.sql").read_text(encoding="utf-8")
        self.assertIn("DELETE FROM [dbo].[ctl_exec_lote]", reset)
        self.assertIn("DELETE FROM [dbo].[ctl_exec_tabela]", reset)
        self.assertIn("DELETE FROM [dbo].[ctl_exec]", reset)
        self.assertNotIn("DELETE FROM [controle_transferencia]", reset)
        self.assertIn("SCHEMA_ID(N'controle_transferencia') IS NOT NULL", verify)
        self.assertIn("OBJECT_ID(N'dbo.ctl_exec_versao', N'U')", verify)
        self.assertIn("@can_alter_dbo <> 0", verify)


if __name__ == "__main__":
    unittest.main()
