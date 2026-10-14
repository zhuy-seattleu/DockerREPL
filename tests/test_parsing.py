"""Step 1: find_code_blocks extracts ```repl blocks from an LM reply."""

import re

from dockerrepl.parsing import REPL_BLOCK_PATTERN, find_code_blocks


def block(code: str, tag: str = "repl") -> str:
    """Wrap `code` in a fenced block with the given language tag."""
    return f"```{tag}\n{code}\n```"


def test_no_blocks_returns_empty_list() -> None:
    assert find_code_blocks("just prose, no code here") == []


def test_single_block() -> None:
    reply = "Let me compute it.\n" + block("x = 1") + "\nDone."
    assert find_code_blocks(reply) == ["x = 1"]


def test_multiple_blocks_in_order() -> None:
    reply = "\n".join(["First:", block("a"), "Then:", block("b"), "Finally:", block("c")])
    assert find_code_blocks(reply) == ["a", "b", "c"]


def test_other_languages_ignored() -> None:
    reply = block("print('not me')", tag="python") + "\n" + block("print('me')")
    assert find_code_blocks(reply) == ["print('me')"]


def test_multiline_code_preserved() -> None:
    code = "total = 0\nfor i in range(3):\n    total += i\nprint(total)"
    assert find_code_blocks(block(code)) == [code]


def test_surrounding_whitespace_stripped() -> None:
    assert find_code_blocks(block("\n\nx = 1\n\n")) == ["x = 1"]


def test_empty_block_skipped() -> None:
    assert find_code_blocks("```repl\n\n```") == []
    assert find_code_blocks(block("   \n\t\n")) == []


def test_quotes_inside_code() -> None:
    code = "a = 'single'\nb = \"double\"\nc = '''triple\nquoted'''"
    assert find_code_blocks(block(code)) == [code]


def test_non_greedy_stops_at_first_fence() -> None:
    reply = block("first = 1") + "\n" + block("second = 2")
    assert find_code_blocks(reply) == ["first = 1", "second = 2"]


def test_block_at_end_of_text() -> None:
    reply = "Here you go:\n" + block("x = 1")
    assert reply.endswith("```")
    assert find_code_blocks(reply) == ["x = 1"]


def test_indented_block_is_dedented_and_compiles() -> None:
    # LMs often indent a whole block when it sits inside a numbered list.
    reply = (
        "1. Run this:\n"
        "   ```repl\n"
        "   x = 1\n"
        "   if x:\n"
        "       print(x)\n"
        "   ```\n"
    )
    [code] = find_code_blocks(reply)
    assert code == "x = 1\nif x:\n    print(x)"
    compile(code, "<repl>", "exec")  # would raise IndentationError if not dedented


def test_crlf_line_endings_normalized() -> None:
    reply = "```repl\r\nx = 1\r\nprint(x)\r\n```"
    assert find_code_blocks(reply) == ["x = 1\nprint(x)"]


def test_unclosed_block_ignored() -> None:
    # A reply cut off mid-block must not run half a program.
    reply = block("x = 1") + "\n```repl\ny = 2\nprint(y"
    assert find_code_blocks(reply) == ["x = 1"]


def test_tag_must_be_exactly_lowercase_repl() -> None:
    assert find_code_blocks(block("x = 1", tag="REPL")) == []
    assert find_code_blocks(block("x = 1", tag="replace")) == []
    assert find_code_blocks("```repl   \nx = 1\n```") == ["x = 1"]  # trailing spaces after the tag are fine


def test_pattern_is_precompiled() -> None:
    assert REPL_BLOCK_PATTERN.flags & re.DOTALL
    assert REPL_BLOCK_PATTERN.pattern.startswith("```repl")
