"""The architecture, as a test.

The map below is the layering contract. It is checked by parsing every module under app/
with ast -- nothing is imported, so a package with a broken dependency still fails here with
a useful message instead of an ImportError from three levels down.

Three properties are enforced:

  1. every app.* import is allowed by ALLOWED;
  2. a new package fails the build until somebody declares what it may import, so adding one
     is a deliberate act rather than a side effect of needing somewhere to put a file;
  3. the graph is acyclic -- a cycle would mean two packages are really one and the split
     between them is decorative.

The single most important row is ``policy``. Because it may import only ``domain`` it cannot
call a model, cannot reach the network, and cannot read a prompt. "The LLM never writes a
commitment" is therefore a property of this file, not a rule anybody has to remember.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

#: What each package may import from app. Widening a row is an architectural decision:
#: say so in the PR, and in docs/DECISION_LOG.md.
ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "config": set(),
    "policy": {"domain"},
    "agent": {"domain"},
    "store": {"domain", "config"},
    "notify": {"domain", "config"},
    "tools": {"domain", "policy", "store", "notify"},
    "vapi": {"domain", "config", "agent", "tools"},
    "api": {"domain", "config", "tools", "store"},
    "jobs": {"domain", "config", "tools", "vapi"},
    # The composition root is the one place allowed to know everything, because wiring the
    # implementations together is the whole of its job. Listed explicitly rather than given a
    # wildcard, so that it stays a visible exception instead of a hidden default.
    "main": {
        "domain",
        "config",
        "policy",
        "agent",
        "store",
        "notify",
        "tools",
        "vapi",
        "api",
        "jobs",
    },
}


def _layer_of(path: Path) -> str:
    """Which layer a file belongs to: its package name, or its stem for a bare module."""
    relative = path.relative_to(APP)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def _app_imports(source: str) -> set[str]:
    """Every app layer this module imports, absolute and relative forms both."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import never leaves its own package, so it cannot violate the
                # contract; it is the absolute forms that can.
                continue
            if node.module and node.module.startswith("app."):
                found.add(node.module.split(".")[1])
    return found


def _modules() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if p.name != "__init__.py" or p.parent != APP)


def test_every_package_declares_its_contract() -> None:
    """A new directory under app/ fails until someone says what it may import."""
    declared = set(ALLOWED)
    actual = {_layer_of(path) for path in _modules()}
    undeclared = actual - declared
    assert not undeclared, (
        f"undeclared layers: {sorted(undeclared)}. Adding a package is an architectural "
        f"decision -- add it to ALLOWED and say why in the PR."
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(APP)))
def test_imports_respect_layering(path: Path) -> None:
    layer = _layer_of(path)
    permitted = ALLOWED[layer]
    for imported in _app_imports(path.read_text(encoding="utf-8")):
        if imported == layer:
            continue
        assert imported in permitted, (
            f"{path.relative_to(APP)} imports app.{imported}, but '{layer}' may only import "
            f"{sorted(permitted) or 'nothing'}. Widening this is an architectural decision, "
            f"not a fix."
        )


def test_policy_is_a_sink() -> None:
    """Stated separately because it is the invariant the whole design exists to protect."""
    assert ALLOWED["policy"] == {"domain"}, (
        "policy/ must import only domain. If it can reach vapi it can call a model; if it "
        "can reach store or notify it can reach the network; if it can reach agent a prompt "
        "can reach it. Each of those turns authorization into something a stranger on the "
        "phone can influence."
    )


def test_layering_map_is_acyclic() -> None:
    """A cycle means two packages are really one, and the split between them is decorative."""
    # main is excluded: it depends on everything by design and would make every graph cyclic.
    graph = {k: v - {"main"} for k, v in ALLOWED.items() if k != "main"}
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        assert node not in visiting, f"import cycle: {' -> '.join([*trail, node])}"
        visiting.add(node)
        for dependency in sorted(graph.get(node, set())):
            walk(dependency, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for layer in sorted(graph):
        walk(layer, [])
