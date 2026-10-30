"""Reuse the existing Python CLI protocol fixtures in one isolated editor."""
import ast
from pathlib import Path
import sys


def cli_program(provider):
    source = Path(__file__).parent / "browser" / (provider + "_research_fixture.py")
    tree = ast.parse(source.read_text())
    call = next(node.value for node in tree.body if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "cli" and node.value.func.attr == "write_text")
    bodies = [node.value for node in ast.walk(call.args[0])
              if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 1000]
    if len(bodies) != 1:
        raise ValueError("The existing CLI fixture must contain one protocol program.")
    body = bodies[0]
    if provider == "deepcode":
        # The executable uses the same temporary profile as the real adapter.
        body = body.replace("from pathlib import Path\n", "from pathlib import Path\n"
                            "Path.home = classmethod(lambda cls: Path(__file__).parent.parent)\n", 1)
    return "#!" + sys.executable + "\n" + body


def cli_profiles(work):
    task_home = Path(work) / "profiles"
    profiles = {name: task_home / ("." + name) for name in ("claude", "codex", "deepcode")}
    configs = {}
    for name, profile in profiles.items():
        profile.mkdir(parents=True, exist_ok=True)
        cli = profile / (name + ("-fixture.py" if name == "deepcode" else "-fixture"))
        cli.write_text(cli_program(name))
        cli.chmod(0o755)
        configs[name] = {"type": name, "label": name.title() + " protocol fixture",
                         "bin": str(cli), "models": ["default", "fast", "deep"], "default_model": "default"}
    return task_home, profiles, configs
