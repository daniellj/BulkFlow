from pathlib import Path
import unicodedata
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCUMENTS = (
    Path("README.md"),
    Path("docs/FALHAS_E_RETOMADA.md"),
    Path("docs/INSTALADOR_OFFLINE.md"),
    Path("docs/MIGRACAO_CONFIGURACAO_V2.md"),
    Path("docs/MIGRACAO_CONTROLE_SQLITE.md"),
    Path("docs/ORDEM_PROCESSAMENTO.md"),
    Path("docs/PRE_REQUISITOS.md"),
    Path("docs/USO_CLI_POWERSHELL_LINUX.md"),
    Path("docs/USO_INTERFACE_GRAFICA.md"),
)


def portuguese_variant(path: Path) -> Path:
    return path.with_name(f"{path.stem}.pt-BR{path.suffix}")


class DocumentationLanguageTests(unittest.TestCase):
    def test_every_canonical_document_has_a_brazilian_portuguese_variant(self):
        for relative_path in CANONICAL_DOCUMENTS:
            with self.subTest(document=str(relative_path)):
                english_path = ROOT / relative_path
                portuguese_path = ROOT / portuguese_variant(relative_path)
                self.assertTrue(english_path.is_file())
                self.assertTrue(portuguese_path.is_file())

                english_header = "\n".join(
                    english_path.read_text(encoding="utf-8").splitlines()[:5]
                )
                portuguese_header = "\n".join(
                    portuguese_path.read_text(encoding="utf-8").splitlines()[:5]
                )
                self.assertIn(portuguese_path.name, english_header)
                self.assertIn(english_path.name, portuguese_header)

    def test_readmes_do_not_link_to_the_unversioned_docker_guide(self):
        for relative_path in (Path("README.md"), Path("README.pt-BR.md")):
            with self.subTest(document=str(relative_path)):
                content = (ROOT / relative_path).read_text(encoding="utf-8")
                normalized_content = "".join(
                    character
                    for character in unicodedata.normalize("NFKD", content)
                    if not unicodedata.combining(character)
                ).lower()
                self.assertNotIn("docker/README.md", content)
                self.assertNotIn("docker\\README.md", content)
                self.assertNotIn("docker/RELATORIO_VALIDACAO_LOCAL.md", content)
                self.assertNotIn("docs/escope/ESCOPO_CODEX_MOTOR_BCP_V2.md", content)
                self.assertNotIn("lab with three SQL Server 2022 instances", content)
                self.assertNotIn(
                    "lab with three sql server 2022 instances", normalized_content
                )
                self.assertNotIn(
                    "laboratorio com tres sql servers 2022", normalized_content
                )

    def test_windows_installer_contains_both_documentation_languages(self):
        product = (ROOT / "packaging" / "wix" / "Product.wxs").read_text(
            encoding="utf-8"
        )
        for relative_path in CANONICAL_DOCUMENTS:
            portuguese_path = portuguese_variant(relative_path)
            for document in (relative_path, portuguese_path):
                with self.subTest(document=str(document)):
                    windows_path = str(document).replace("/", "\\")
                    self.assertIn(windows_path, product)


if __name__ == "__main__":
    unittest.main()
