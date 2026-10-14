# Step 1 — Extract `repl` code blocks

## Goal

A pure function that finds every ```` ```repl ```` code block in an LM reply
and returns the code inside, in order, ready to run. The Client (step 9) calls
it on every reply. MockLM's tests (step 2) use it to check their scripted
replies.

## Tasks at a glance

1. **Define the block format as one regular expression.** Write
   `REPL_BLOCK_PATTERN` in `dockerrepl/parsing.py`, compiled once at module
   level. It matches an opening ```` ```repl ```` fence, the code, and the
   closing ```` ``` ````. The closing fence may be indented.
2. **Implement `find_code_blocks(text)`.** It normalises Windows line endings,
   finds every match in order, removes the indentation shared by all lines
   with `textwrap.dedent`, drops blank lines at both ends, and skips empty
   blocks.
3. **Handle real LM output safely.** Indented blocks (inside a numbered list)
   are found and dedented, so they compile. A block with no closing fence, as
   in a reply cut off mid-code, is ignored, so half a program never runs.
4. **Write the unit tests.** `tests/test_parsing.py` has 15 tests covering
   normal replies, edge cases, and the mistakes in the Pitfalls section.
5. **Check that the tests catch real bugs.** Temporarily make each mistake
   from the Pitfalls section and watch the matching test fail.

**Outcome:** `pytest -q` shows 15 more passing tests, and the CI table shows
step 1 as ✅.

## What you'll learn

* **Regular expressions:** non-greedy matching (`.*?`), the `re.DOTALL` flag so
  `.` also matches newlines, character classes like `[ \t]*`, and
  `re.finditer`.
* **Why "almost right" parsing breaks code:** stripping only the first line's
  indentation turns valid Python into an `IndentationError`.
* **Writing small, pure functions** that are easy to test exhaustively.
* **Mutation checking:** a test is only useful if it fails when the code is
  wrong.

## Depends on

Step 0.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/parsing.py` | `REPL_BLOCK_PATTERN`, `find_code_blocks` |
| `tests/test_parsing.py` | the tests below |

## Interface

```python
REPL_BLOCK_PATTERN: re.Pattern[str]
    # re.compile(r"```repl[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)

def find_code_blocks(text: str) -> list[str]:
    """Return the code of every ```repl block in `text`, in order.

    Each block's common indentation is removed and blank lines at its start
    and end are dropped. Empty blocks, and a final block with no closing
    fence (a truncated reply), are skipped.
    """
```

## Implementation guide

1. **Compile the pattern once at module level.** Piece by piece:

   | Part | Meaning |
   |---|---|
   | ```` ```repl ```` | the opening fence and its tag, which must be lowercase `repl` |
   | `[ \t]*\n` | optional spaces or tabs after the tag, then the end of that line |
   | `(.*?)` | the code. **Non-greedy**, so it stops at the *first* closing fence |
   | `\n[ \t]*```` ` | a newline, optional indentation, then the closing fence |
   | `re.DOTALL` | lets `.` match newlines, so the code can span many lines |

2. **In `find_code_blocks`:**
   1. `text = text.replace("\r\n", "\n")` normalises Windows line endings.
   2. Loop over `REPL_BLOCK_PATTERN.finditer(text)`.
   3. For each match: `code = textwrap.dedent(match.group(1)).strip("\n")`.
      `dedent` removes the indentation that **every** line shares, and also
      turns whitespace-only lines into empty ones. `strip("\n")` then removes
      blank lines at both ends without touching the first line's indentation.
   4. Keep `code` only if `code.strip()` isn't empty.

## Tests — `tests/test_parsing.py`

A one-line helper keeps the inputs readable:
`block(code, tag="repl") -> f"```{tag}\n{code}\n```"`.

| Test | Input (sketch) | Expected |
|---|---|---|
| `test_no_blocks_returns_empty_list` | `"just prose"` | `[]` |
| `test_single_block` | prose + one block with `x = 1` | `["x = 1"]` |
| `test_multiple_blocks_in_order` | three blocks `a`, `b`, `c` with prose between | `["a", "b", "c"]` |
| `test_other_languages_ignored` | a ```` ```python ```` block and a `repl` block | only the `repl` code |
| `test_multiline_code_preserved` | a `for` loop with an indented body | exactly the same lines |
| `test_surrounding_whitespace_stripped` | blank lines before and after the code | the code without them |
| `test_empty_block_skipped` | ```` ```repl\n\n``` ````, and a block of only spaces and tabs | `[]` |
| `test_quotes_inside_code` | `'`, `"` and `'''` inside the code | unchanged |
| `test_non_greedy_stops_at_first_fence` | two adjacent blocks | two entries, not one merged block |
| `test_block_at_end_of_text` | text ending right after the closing fence | the block is found |
| `test_indented_block_is_dedented_and_compiles` | a block indented 3 spaces inside a numbered list, closing fence indented too | the dedented code, which `compile()` accepts |
| `test_crlf_line_endings_normalized` | the same block with `\r\n` line endings | `\n` line endings |
| `test_unclosed_block_ignored` | one complete block, then ```` ```repl ```` with no closing fence | only the complete block |
| `test_tag_must_be_exactly_lowercase_repl` | ```` ```REPL ````, ```` ```replace ````, and ```` ```repl ```` followed by spaces | `[]`, `[]`, and the code |
| `test_pattern_is_precompiled` | `REPL_BLOCK_PATTERN` itself | has `re.DOTALL` and starts with ```` ```repl ```` |

## Run

```bash
pytest -v tests/test_parsing.py
```

## Done when

- [ ] All 15 tests pass, and the step 0 tests still pass.
- [ ] Each mistake in the Pitfalls section, made on purpose, makes at least one
      test fail. Undo it afterwards.
- [ ] `find_code_blocks` has no side effects and imports only `re` and
      `textwrap`.
- [ ] The **Tests** workflow on GitHub shows step 1 as ✅.

## Pitfalls

Each of these was checked: making the mistake makes the listed tests fail.

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Greedy `(.*)`** | the first and last blocks merge into one, swallowing the prose between them | `test_non_greedy_stops_at_first_fence`, `test_multiple_blocks_in_order`, `test_unclosed_block_ignored` |
| **Forgetting `re.DOTALL`** | `.` stops at newlines, so only one-line blocks match | `test_multiline_code_preserved` and 5 others |
| **`.strip()` instead of `dedent` + `strip("\n")`** | an indented block loses the first line's indentation only, giving `IndentationError: unexpected indent` | `test_indented_block_is_dedented_and_compiles` |

One more thing to know: a line inside the code that starts with ```` ``` ````
ends the block early, for example in a multi-line string holding Markdown.
That's a limit of the simple fence format, and acceptable for this project.
