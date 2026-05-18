import ast
from pathlib import Path


def test_omni_agent_init_does_not_assign_duplicate_subsystems():
    tree = ast.parse(Path("agent/core.py").read_text(encoding="utf-8"))

    class InitVisitor(ast.NodeVisitor):
        def __init__(self):
            self.targets = []

        def visit_FunctionDef(self, node):
            if node.name != "__init__":
                return

            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            self.targets.append(target.attr)

    visitor = InitVisitor()
    visitor.visit(tree)

    duplicates = {
        name for name in set(visitor.targets)
        if visitor.targets.count(name) > 1
    }

    assert not duplicates, (
        "Duplicate self assignments in OmniAgent.__init__: "
        f"{sorted(duplicates)}"
    )
