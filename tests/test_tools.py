"""Step 5: validating and describing custom_tools."""

import pytest

from dockerrepl.environments.tools import (
    RESERVED_TOOL_NAMES,
    format_tools_for_prompt,
    is_code_string,
    parse_tool_entry,
    tool_kind,
    validate_custom_tools,
)

TRIPLE = "def triple(x):\n    return x * 3"


def problems_for(tools: dict) -> str:
    """The ValueError message validate_custom_tools raises for `tools`."""
    with pytest.raises(ValueError) as info:
        validate_custom_tools(tools)
    return str(info.value)


# ---------------------------------------------------------------- parsing and kinds


def test_wrapped_entry_parsed() -> None:
    assert parse_tool_entry({"tool": 7, "description": "seven"}) == (7, "seven")


def test_wrapped_entry_without_description() -> None:
    assert parse_tool_entry({"tool": 7}) == (7, None)


def test_plain_dict_is_data() -> None:
    assert parse_tool_entry({"a": 1}) == ({"a": 1}, None)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (TRIPLE, "code"),
        ("def f(): return 1", "code"),
        ("class C: pass", "code"),
        ("async def f(): pass", "code"),
        ("import math\nPI = math.pi", "code"),
        ("lambda x: x * 3", "lambda"),
        ("lambda:0", "lambda"),
        ("hello", "data"),
        ("x = 5", "data"),  # one line, no def/class/lambda: just a string
        (7, "data"),
        ([1, 2], "data"),
        ({"a": 1}, "data"),
    ],
)
def test_tool_kind(value: object, kind: str) -> None:
    assert tool_kind(value) == kind


def test_plain_string_is_data() -> None:
    assert not is_code_string("hello")
    assert is_code_string(TRIPLE)
    assert is_code_string("lambda x: x")


# ---------------------------------------------------------------- valid tools


def test_none_and_empty_are_valid() -> None:
    validate_custom_tools(None)
    validate_custom_tools({})


def test_code_strings_accepted() -> None:
    validate_custom_tools(
        {
            "triple": TRIPLE,
            "Point": "class Point:\n    pass",
            "double": "lambda x: x * 2",
            "area": "import math\ndef area(r):\n    return math.pi * r * r",
            "PI": "import math\nPI = math.pi",
            "np": "import json as np\n",  # bound by "import ... as"
        }
    )


def test_data_accepted() -> None:
    validate_custom_tools(
        {"CONST": 7, "LOOKUP": {"a": [1, 2]}, "NAME": "Ada", "FLAG": True, "NOTHING": None}
    )


def test_wrapped_entries_accepted() -> None:
    validate_custom_tools({"triple": {"tool": TRIPLE, "description": "triples a number"}, "N": {"tool": 3}})


# ---------------------------------------------------------------- names


def test_reserved_name_rejected() -> None:
    assert "llm_query" in problems_for({"llm_query": "x"})


@pytest.mark.parametrize("name", sorted(RESERVED_TOOL_NAMES))
def test_each_reserved_name_rejected(name: str) -> None:
    assert "reserved" in problems_for({name: 1})


@pytest.mark.parametrize("name", ["my-tool", "2x", "", "has space"])
def test_invalid_identifier_rejected(name: str) -> None:
    assert "identifier" in problems_for({name: 1})


def test_keyword_name_rejected() -> None:
    assert "class".isidentifier()  # why isidentifier() alone isn't enough
    assert "keyword" in problems_for({"class": 1})


def test_non_dict_rejected() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        validate_custom_tools([("x", 1)])  # type: ignore[arg-type]


# ---------------------------------------------------------------- bad values


def test_callable_rejected_with_hint() -> None:
    message = problems_for({"f": len, "g": lambda x: x})
    assert "'f'" in message and "'g'" in message
    assert "code string" in message


def test_non_json_data_rejected() -> None:
    assert "JSON" in problems_for({"s": {1, 2}})


@pytest.mark.parametrize("value", [(1, 2), {1: "a"}, [(1, 2)]])
def test_data_that_changes_in_json_rejected(value: object) -> None:
    assert "would change" in problems_for({"v": value})


def test_nan_rejected() -> None:
    # NaN isn't valid JSON; the message should say so, not "would change".
    assert "JSON-serialisable" in problems_for({"v": float("nan")})


def test_code_with_syntax_error_rejected() -> None:
    message = problems_for({"triple": "def triple(x)\n    return x * 3"})  # missing colon
    assert "not valid Python" in message and "line 1" in message


def test_code_not_defining_its_name_rejected() -> None:
    assert "doesn't define 'triple'" in problems_for({"triple": "def tripel(x):\n    return x * 3"})


def test_lambda_must_be_one_expression() -> None:
    assert "not valid Python" in problems_for({"f": "lambda x: x\nprint(1)"})


def test_text_starting_with_lambda_rejected() -> None:
    assert "not valid Python" in problems_for({"note": "lambda is a Greek letter"})


def test_multiline_text_gets_hint() -> None:
    message = problems_for({"poem": "Roses are red,\nviolets are blue"})
    assert "list of lines" in message


def test_description_must_be_string() -> None:
    assert "description" in problems_for({"x": {"tool": 1, "description": 42}})


def test_all_errors_reported_together() -> None:
    message = problems_for({"my-tool": 1, "llm_query": 1, "f": len, "ok": 3, "s": {1}})
    for name in ("'my-tool'", "'llm_query'", "'f'", "'s'"):
        assert name in message
    assert "'ok'" not in message


# ---------------------------------------------------------------- prompt text


def test_format_with_descriptions() -> None:
    text = format_tools_for_prompt({"triple": {"tool": TRIPLE, "description": "triples a number"}})
    assert text == "- triple: triples a number"


def test_format_defaults() -> None:
    text = format_tools_for_prompt(
        {
            "triple": TRIPLE,
            "double": "lambda x: 2 * x",
            "Point": "class Point:\n    pass",
            "CONST": 7,
            "NAMES": ["a"],
        }
    )
    assert text.splitlines() == [
        "- triple: a custom function",
        "- double: a custom function",
        "- Point: a custom class",
        "- CONST: a custom int value",
        "- NAMES: a custom list value",
    ]


def test_format_none_when_empty() -> None:
    assert format_tools_for_prompt(None) is None
    assert format_tools_for_prompt({}) is None
