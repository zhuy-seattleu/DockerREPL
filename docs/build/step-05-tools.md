# Step 5 — `custom_tools` validation and description

## Goal

Pure functions that check a `custom_tools` dict, sort each tool into one of
three kinds (**code**, **lambda** or **data**), and describe the tools for the
system prompt. Steps 6, 8 and 9 use them. Nothing here touches Docker or the
network, and nothing here runs the tools' code: code is only **parsed**.

The point of this step is to catch every mistake in `custom_tools` **on the
host, before a container starts**, with a clear message. The same mistake
found later, inside the container, is slower to hit and harder to understand.

## Tasks at a glance

1. **Unwrap entries.** `parse_tool_entry` turns `{"tool": v, "description": d}`
   into `(v, d)` and anything else into `(entry, None)`.
2. **Classify values.** `tool_kind` returns `"code"` (a string starting with
   `def`/`class`/`async def`, or containing a newline), `"lambda"` (a string
   starting with `lambda`), or `"data"` (anything else).
3. **Validate everything at once.** `validate_custom_tools` checks every tool
   and raises **one** `ValueError` listing all the problems:
   - **names:** a valid identifier, not a keyword, not reserved;
   - **no host functions:** a callable gets a hint to pass its source instead;
   - **code:** valid Python that defines the tool's name, checked with `ast`;
   - **lambdas:** a single lambda expression;
   - **data:** survives a JSON round trip unchanged.
4. **Describe tools for the LM.** `format_tools_for_prompt` writes one line
   per tool, with a sensible default when there's no description.
5. **Write the unit tests.** `tests/test_tools.py` has 28 test functions
   (47 test cases, counting parametrized ones).
6. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 47 more passing test cases, and the CI table
shows step 5 as ✅.

## What you'll learn

* **Validating input early**, and reporting **all** problems at once, before
  any expensive work such as starting a container.
* **Why a host Python function can't be sent to another process**, and what
  to send instead (its source code).
* **The `ast` module:** checking that code parses, and what names it defines,
  **without running it**.
* **`exec` vs `eval`:** `exec("lambda x: x")` builds a function and throws it
  away; `eval` returns it so you can bind it to a name.
* **JSON's limits:** tuples, non-string dict keys and `NaN` don't survive a
  round trip, so "it serialised" isn't the same as "it arrives unchanged".
* **Python keywords vs identifiers:** `"class".isidentifier()` is `True`, but
  you can't assign to `class`.

## Depends on

Step 0.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/tools.py` | the functions below |
| `tests/test_tools.py` | the tests below |

## Interface

```python
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {"llm_query", "llm_query_batched", "SHOW_VARS", "answer"})

ToolKind = Literal["code", "lambda", "data"]

def parse_tool_entry(entry: Any) -> tuple[Any, str | None]: ...
    # {"tool": v, "description": d} → (v, d); anything else → (entry, None)

def tool_kind(value: Any) -> ToolKind: ...
    # by form only: "lambda" (starts with lambda), "code" (starts with def/class/async def,
    # or contains a newline), else "data"

def is_code_string(value: Any) -> bool: ...
    # tool_kind(value) != "data"

def validate_custom_tools(custom_tools: dict[str, Any] | None) -> None: ...
    # raises one ValueError listing every problem; returns None when valid

def format_tools_for_prompt(custom_tools: dict[str, Any] | None) -> str | None: ...
```

How step 6 will use each kind in the container:

| Kind | Example | In the container |
|---|---|---|
| code | `"def triple(x):\n    return x * 3"` | `exec(code, ns)` defines `triple` |
| lambda | `"lambda x: 2 * x"` | `ns["double"] = eval(code, ns)` |
| data | `7`, `{"a": [1, 2]}`, `"Ada"` | `ns["CONST"] = json.loads(...)` |

A one-line string such as `"x = 5"` is **data**: just the text `"x = 5"`.
Multi-line text counts as code, so to pass text, use a list of lines.

## Implementation guide

1. **`parse_tool_entry`** only unwraps a dict that has a `"tool"` key. Any
   other dict, such as `{"a": 1}`, is plain data.
2. **`tool_kind`:** non-strings are `"data"`. For strings, strip whitespace,
   then:
   - starts with `"lambda"` → `"lambda"`. Match `"lambda"` without a space:
     `lambda:0` is valid Python.
   - starts with `"def "`, `"class "` or `"async def "`, or contains `"\n"`
     → `"code"`.
   - otherwise → `"data"`.
3. **`validate_custom_tools`:**
   - `None` and `{}` are valid. Anything other than a `dict` raises at once.
   - For each `name, entry`, append a problem to a list instead of raising,
     and use `continue` where later checks would be meaningless:
     1. `name` must be a `str`, satisfy `name.isidentifier()`, and **not** be
        a keyword (`keyword.iskeyword(name)`).
     2. `name` must not be in `RESERVED_TOOL_NAMES`.
     3. Unwrap with `parse_tool_entry`. A description, if given, must be a
        `str`.
     4. **Check `callable(value)` before any JSON check**, so the message can
        say "pass its source as a code string".
     5. **Code:** `tree = ast.parse(value.strip())`. A `SyntaxError` gives
        "not valid Python: line N: …"; for multi-line strings, add the hint
        to use a list of lines for text. Then collect the names the module
        binds at the top level (`FunctionDef`, `AsyncFunctionDef`,
        `ClassDef`, `Assign`/`AnnAssign`/`AugAssign` targets, and
        `Import`/`ImportFrom` names or their `as` names). The tool's name
        must be among them.
     6. **Lambda:** `ast.parse(value.strip(), mode="eval")` must succeed, and
        its `.body` must be an `ast.Lambda`.
     7. **Data:** `text = json.dumps(value, allow_nan=False)` must not raise
        (`TypeError` or `ValueError`), and `json.loads(text) == value` must
        hold.
   - If the list isn't empty, raise **one** `ValueError` with every problem,
     one per line. Every problem starts with `tool '<name>':`.
4. **`format_tools_for_prompt`:** `None` for no tools; otherwise one line per
   tool, in the dict's order: `- name: description`. Without a description:
   `a custom function` (code or lambda), `a custom class` (code starting with
   `class`), or `a custom <type> value` for data, such as
   `a custom int value`.

## Tests — `tests/test_tools.py`

A helper `problems_for(tools)` returns the text of the `ValueError` that
`validate_custom_tools` raises. `TRIPLE` is
`"def triple(x):\n    return x * 3"`.

| Test | Checks |
|---|---|
| `test_wrapped_entry_parsed` | `parse_tool_entry({"tool": 7, "description": "seven"}) == (7, "seven")` |
| `test_wrapped_entry_without_description` | `{"tool": 7}` gives `(7, None)` |
| `test_plain_dict_is_data` | `parse_tool_entry({"a": 1}) == ({"a": 1}, None)` |
| `test_tool_kind` (12 cases) | `def`/`class`/`async def`/multi-line → code; `lambda x: …` and `lambda:0` → lambda; `"hello"`, `"x = 5"`, `7`, a list and a dict → data |
| `test_plain_string_is_data` | `is_code_string` is `False` for `"hello"` and `True` for code and lambdas |
| `test_none_and_empty_are_valid` | `None` and `{}` don't raise |
| `test_code_strings_accepted` | a function, a class, a lambda, imports plus a function, imports plus an assignment, and `import json as np` all pass |
| `test_data_accepted` | an int, a nested dict, a string, a bool and `None` all pass |
| `test_wrapped_entries_accepted` | wrapped code and wrapped data pass |
| `test_reserved_name_rejected` | `{"llm_query": "x"}`: the message names `llm_query` |
| `test_each_reserved_name_rejected` (4 cases) | every reserved name gives "reserved" |
| `test_invalid_identifier_rejected` (4 cases) | `"my-tool"`, `"2x"`, `""`, `"has space"` |
| `test_keyword_name_rejected` | `"class"` passes `isidentifier()` but is rejected as a keyword |
| `test_non_dict_rejected` | a list instead of a dict raises "must be a dict" |
| `test_callable_rejected_with_hint` | `len` and a `lambda` **object** are both named, with the hint "code string" |
| `test_non_json_data_rejected` | a set is rejected, mentioning JSON |
| `test_data_that_changes_in_json_rejected` (3 cases) | a tuple, `{1: "a"}`, and a list of tuples: "would change" |
| `test_nan_rejected` | `NaN` is rejected as not "JSON-serialisable" |
| `test_code_with_syntax_error_rejected` | a missing colon: "not valid Python" and "line 1" |
| `test_code_not_defining_its_name_rejected` | `{"triple": "def tripel(x): …"}`: "doesn't define 'triple'" |
| `test_lambda_must_be_one_expression` | a lambda followed by another line is rejected |
| `test_text_starting_with_lambda_rejected` | `"lambda is a Greek letter"` is rejected, not silently run |
| `test_multiline_text_gets_hint` | two lines of prose: the message suggests a list of lines |
| `test_description_must_be_string` | `"description": 42` is rejected |
| `test_all_errors_reported_together` | 4 bad tools (bad name, reserved, callable, set) and 1 good one: all 4 are named, the good one isn't |
| `test_format_with_descriptions` | `"- triple: triples a number"` |
| `test_format_defaults` | function, lambda, class, int and list defaults, in the dict's order |
| `test_format_none_when_empty` | `None` and `{}` give `None` |

## Run

```bash
pytest -v tests/test_tools.py
```

## Done when

- [ ] All 47 test cases pass, and steps 0–4 still pass.
- [ ] Every problem message starts with `tool '<name>':`.
- [ ] No test or function **runs** a tool's code; it's only parsed.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 5 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Treating every dict as a wrapped entry** | `{"a": 1}` becomes `None` | `test_plain_dict_is_data` and 2 others |
| **Matching `"lambda "` with a space** | `lambda:0` is treated as data | `test_tool_kind[lambda:0-lambda]` |
| **Checking JSON before callables** | the message says "not JSON-serialisable" instead of how to fix it | `test_callable_rejected_with_hint` |
| **`isidentifier()` without a keyword check** | `class` is accepted, then fails in the container | `test_keyword_name_rejected` |
| **Only checking that `json.dumps` works** | tuples and integer keys arrive changed | `test_data_that_changes_in_json_rejected` (3 cases) |
| **Not checking the code defines the name** | a typo like `def tripel` fails only in the container | `test_code_not_defining_its_name_rejected` |
| **Parsing a lambda as a statement** | a valid lambda "doesn't define" its name | `test_code_strings_accepted`, `test_lambda_must_be_one_expression` |
| **Raising at the first problem** | the user fixes one mistake per run | `test_all_errors_reported_together` |
| **Allowing `NaN`** | it's rejected with the misleading "would change" message, because `NaN != NaN` | `test_nan_rejected` |

One more thing to know: **never `exec` a tool's code on the host to check
it.** It may import packages the host doesn't have, or do something the host
shouldn't. `ast.parse` answers "is it valid, and what does it define?" without
running anything.
