"""Decision-layer dependency boundary tests."""

import ast
import unittest
from pathlib import Path

DECISION_DIR = Path("src/rugbot/decision")
FORBIDDEN_IMPORT_PREFIXES = (
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "rugbot.protocol",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
)


class DecisionBoundaryTests(unittest.TestCase):
    """Tests for decision-layer import boundaries."""

    def test_decision_modules_do_not_import_infrastructure(self) -> None:
        """Decision logic remains pure and adapter-free."""

        violations: list[str] = []
        for path in DECISION_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for imported_name in _imported_module_names(tree):
                if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path}:{imported_name}")

        self.assertEqual(violations, [])


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


if __name__ == "__main__":
    unittest.main()
