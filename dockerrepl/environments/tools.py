"""Validate and describe `custom_tools`: functions and data made available in the REPL.

A tool entry is either a plain value or {"tool": value, "description": "..."}.
Each value is one of three kinds:

* "code"   -- a string of Python that defines the tool's name (def, class, or
              several lines), run with exec() in the container;
* "lambda" -- a string starting with "lambda", evaluated and bound to the name;
* "data"   -- anything else; it must survive a JSON round trip unchanged.

Host functions (callables) can't be sent to the container, so they're rejected
with a hint to pass the source code as a string instead. Everything is checked
here, on the host, before a container is started; code is parsed with `ast`,
never run.
"""

import ast
import json
import keyword
from typing import Any, Literal

RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {"llm_query", "llm_query_batched", "SHOW_VARS", "answer"}
)

ToolKind = Literal["code", "lambda", "data"]

_CODE_PREFIXES = ("def ", "class ", "async def ")


def parse_tool_entry(entry: Any) -> tuple[Any, str | None]:
    """Split an entry into (value, description). Only a dict with a "tool" key is a wrapper."""
    if isinstance(entry, dict) and "tool" in entry:
        return entry["tool"], entry.get("description")
    return entry, None


def tool_kind(value: Any) -> ToolKind:
    """Classify a tool value as "code", "lambda" or "data" (by its form, not by validity)."""
    if not isinstance(value, str):
        return "data"
    text = value.strip()
    if text.startswith("lambda"):
        return "lambda"
    if text.startswith(_CODE_PREFIXES) or "\n" in text:
        return "code"
    return "data"


def is_code_string(value: Any) -> bool:
    """True if the value is a code or lambda string, i.e. not plain data."""
    return tool_kind(value) != "data"


def _names_defined(tree: ast.Module) -> set[str]:
    """Names bound at the top level of a module: def, class, assignment, import."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _check_code(name: str, value: str, kind: ToolKind) -> str | None:
    """Return a problem description for a code/lambda string, or None if it's fine."""
    mode = "eval" if kind == "lambda" else "exec"
    try:
        tree = ast.parse(value.strip(), mode=mode)
    except SyntaxError as e:
        hint = ""
        if kind == "code" and "\n" in value.strip():
            hint = " (multi-line strings are treated as code; to pass text, use a list of lines instead)"
        return f"tool {name!r}: not valid Python: line {e.lineno}: {e.msg}{hint}"
    if kind == "lambda":
        if not isinstance(tree.body, ast.Lambda):
            return f"tool {name!r}: a string starting with 'lambda' must be a single lambda expression"
        return None
    if name not in _names_defined(tree):  # type: ignore[arg-type]
        return f"tool {name!r}: the code doesn't define {name!r} (check the spelling of the def/class name)"
    return None


def _check_data(name: str, value: Any) -> str | None:
    """Return a problem description for a data value, or None if it survives JSON unchanged."""
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as e:
        return f"tool {name!r}: data must be JSON-serialisable ({e})"
    if json.loads(text) != value:
        return (
            f"tool {name!r}: data would change when sent as JSON "
            "(for example tuples become lists, and dict keys become strings)"
        )
    return None


def validate_custom_tools(custom_tools: dict[str, Any] | None) -> None:
    """Check every tool; raise one ValueError listing all problems, or return None."""
    if custom_tools is None:
        return
    if not isinstance(custom_tools, dict):
        raise ValueError(f"custom_tools must be a dict, not {type(custom_tools).__name__}")

    problems: list[str] = []
    for name, entry in custom_tools.items():
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            problems.append(f"tool {name!r}: name must be a valid Python identifier, not a keyword")
            continue
        if name in RESERVED_TOOL_NAMES:
            reserved = ", ".join(sorted(RESERVED_TOOL_NAMES))
            problems.append(f"tool {name!r}: name is reserved (reserved: {reserved})")
            continue
        value, description = parse_tool_entry(entry)
        if description is not None and not isinstance(description, str):
            problems.append(f"tool {name!r}: description must be a string")
        if callable(value):
            problems.append(
                f"tool {name!r}: a Python function can't be sent to the container; "
                "pass its source as a code string instead, e.g. \"def f(x):\\n    return x\""
            )
            continue
        kind = tool_kind(value)
        problem = _check_data(name, value) if kind == "data" else _check_code(name, value, kind)
        if problem:
            problems.append(problem)

    if problems:
        raise ValueError("Invalid custom_tools:\n  " + "\n  ".join(problems))


def format_tools_for_prompt(custom_tools: dict[str, Any] | None) -> str | None:
    """One line per tool for the system prompt, or None if there are no tools."""
    if not custom_tools:
        return None
    lines = []
    for name, entry in custom_tools.items():
        value, description = parse_tool_entry(entry)
        if description is None:
            kind = tool_kind(value)
            if kind == "data":
                description = f"a custom {type(value).__name__} value"
            elif kind == "code" and value.strip().startswith("class "):
                description = "a custom class"
            else:
                description = "a custom function"
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)
