"""Protect the command registry from silently shadowed duplicate definitions."""
import ast
from collections import Counter
from pathlib import Path

from omm import cli


def test_cli_has_no_shadowed_top_level_definitions():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    names = Counter(node.name for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef)))
    assert {name: count for name, count in names.items() if count > 1} == {}


def test_cli_registers_each_top_level_command_once():
    names = [command.name or command.callback.__name__
             for command in cli.app.registered_commands]
    assert len(names) == len(set(names))
