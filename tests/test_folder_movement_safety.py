import ast
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]


def function_contains_call(path, function_name, called_name):
    tree = ast.parse((WORKTREE / path).read_text())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function_name)
    return any(
        isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == called_name)
             or (isinstance(node.func, ast.Attribute) and node.func.attr == called_name))
        for node in ast.walk(function)
    )


class FolderMovementSafetyTest(unittest.TestCase):
    def test_metadata_refresh_never_renames_a_series_folder(self):
        self.assertFalse(function_contains_call(
            'blueprints/bedetheque/routes.py',
            '_align_title_and_start_metadata_write',
            '_rename_series_folder',
        ))

    def test_universe_assignment_reconciles_the_series_folder(self):
        self.assertTrue(function_contains_call(
            'blueprints/library/routes.py',
            'update_series_manual_metadata',
            '_move_series_folder_for_universe',
        ))


if __name__ == '__main__':
    unittest.main()
