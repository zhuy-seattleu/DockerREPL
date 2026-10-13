# Step 5 — `custom_tools` validation and description

## Goal

Pure functions that check a `custom_tools` dict, sort each tool into *code*
or *data*, and describe the tools for the system prompt. Steps 6, 8 and 9 use
them. Nothing here touches Docker or the network.

## What you'll learn

* **Validating input early**, with clear error messages, before any expensive
  work such as starting a container.
* **Why a host Python function can't be sent to another process** (here, the
  container), and what to send instead.
* **Checking JSON-serialisability** with `json.dumps` and catching `TypeError`.

## Depends on

Step 0.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/tools.py` | the functions below |
| `tests/test_tools.py` | tests below |

## Interface

```python
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {"llm_query", "llm_query_batched", "SHOW_VARS", "answer"})

def parse_tool_entry(entry: Any) -> tuple[Any, str | None]: ...
    # {"tool": v, "description": d} → (v, d); anything else → (entry, None)

def is_code_string(value: Any) -> bool: ...
    # str that starts with "def ", "class " or "lambda" (after lstrip), or contains "\n"

def validate_custom_tools(custom_tools: dict[str, Any] | None) -> None: ...
    # raises ValueError; returns None when valid

def format_tools_for_prompt(custom_tools: dict[str, Any] | None) -> str | None: ...
```

## Implementation guide

1. **`parse_tool_entry`** only unwraps a dict that has a `"tool"` key. Any
   other dict is plain data.
2. **`validate_custom_tools`**, for each `name, entry` (`None` and `{}` are
   valid):
   - `name` must be a valid Python identifier (`name.isidentifier()`), so it
     can become a variable.
   - `name` must not be in `RESERVED_TOOL_NAMES`.
   - The unwrapped value must not be callable. The error message must suggest
     passing it as a code string.
   - If it isn't a code string, `json.dumps(value)` must succeed.
   - The description, if given, must be a `str`.
   - Collect every problem, then raise one `ValueError` listing all of them.
     That is friendlier than stopping at the first.
3. **`format_tools_for_prompt`:**
   - Return `None` for no tools.
   - Otherwise return one line per tool:
     `- name: description`. When there's no description, use
     `- name: a custom function` for code strings, and
     `- name: a custom <type> value` for data, for example `a custom int value`.

## Tests — `tests/test_tools.py`

| Test | Checks |
|---|---|
| `test_none_and_empty_are_valid` | `validate_custom_tools(None)` and `({})` don't raise |
| `test_reserved_name_rejected` | `{"llm_query": "x"}` raises `ValueError` mentioning `llm_query` |
| `test_each_reserved_name_rejected` | the same for all 4 names (use `pytest.mark.parametrize`) |
| `test_invalid_identifier_rejected` | `{"my-tool": 1}` raises |
| `test_callable_rejected_with_hint` | `{"f": len}` and `{"g": lambda x: x}` raise, and the message contains `"code string"` |
| `test_non_json_data_rejected` | `{"s": {1, 2}}` (a set) raises |
| `test_code_strings_accepted` | `"def f(): ..."`, `"class C: ..."`, `"lambda x: x"` and a multi-line string all pass |
| `test_plain_string_is_data` | `is_code_string("hello")` is `False` |
| `test_wrapped_entry_parsed` | `parse_tool_entry({"tool": 7, "description": "seven"}) == (7, "seven")` |
| `test_plain_dict_is_data` | `parse_tool_entry({"a": 1}) == ({"a": 1}, None)` |
| `test_all_errors_reported_together` | two bad tools: the single `ValueError` names both |
| `test_format_with_descriptions` | a line contains the name and its description |
| `test_format_defaults` | code without a description gives `"a custom function"`; `7` gives `"a custom int value"` |
| `test_format_none_when_empty` | `None` or `{}` gives `None` |

## Run

```bash
pytest -q tests/test_tools.py
```

## Done when

- [ ] All tests pass, and steps 0–4 still pass.
- [ ] Every error message names the tool it is about.

## Pitfalls

* **Treating every dict as a wrapped entry.** Only `{"tool": ...}` is one.
  `{"a": 1}` is data.
* **`"lambda"` vs `"lambda "`.** `lambda:0` (no space) is valid Python, so
  match the prefix `"lambda"` without the space.
* **Checking JSON before callables.** `json.dumps(len)` also raises
  `TypeError`, which would give a vague "not JSON-serialisable" message.
  Check `callable()` first, so the error says to pass a code string.
