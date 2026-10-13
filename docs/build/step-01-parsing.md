# Step 1 — Extract `repl` code blocks

## Goal

A pure function that finds every ```` ```repl ```` code block in an LM reply
and returns the code inside, in order. The Client (step 9) uses it on every
reply. MockLM's tests (step 2) use it to check their scripted replies.

## What you'll learn

* Regular expressions: non-greedy matching (`.*?`), and the `re.DOTALL` flag
  so `.` also matches newlines.
* Writing small, pure functions that are easy to test exhaustively.

## Depends on

Step 0.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/parsing.py` | `REPL_BLOCK_PATTERN`, `find_code_blocks` |
| `tests/test_parsing.py` | tests below |

## Interface

```python
REPL_BLOCK_PATTERN: re.Pattern[str]      # re.compile(r"```repl\s*\n(.*?)\n```", re.DOTALL)

def find_code_blocks(text: str) -> list[str]:
    """Return the code of every ```repl block in `text`, in order.
    Each block's code is stripped of surrounding whitespace; empty blocks are skipped."""
```

## Implementation guide

1. Compile the pattern once at module level:
   - `` ```repl `` matches the opening fence and its tag, which must be
     lowercase `repl`.
   - `\s*\n` allows trailing spaces after the tag, then needs a newline.
   - `(.*?)` captures the code. It is non-greedy, so it stops at the *first*
     closing fence.
   - `\n```` ` is the closing fence, which must start on its own line.
2. Use `REPL_BLOCK_PATTERN.finditer(text)`, take `match.group(1).strip()` from
   each match, and drop empty strings.

## Tests — `tests/test_parsing.py`

| Test | Input (sketch) | Expected |
|---|---|---|
| `test_no_blocks_returns_empty_list` | `"just prose"` | `[]` |
| `test_single_block` | prose + one `repl` block with `x = 1` | `["x = 1"]` |
| `test_multiple_blocks_in_order` | three blocks `a`, `b`, `c` with prose between | `["a", "b", "c"]` |
| `test_other_languages_ignored` | a ```` ```python ```` block and a `repl` block | only the `repl` code |
| `test_multiline_code_preserved` | a block with a `for` loop and indented body | the exact lines, indentation kept |
| `test_surrounding_whitespace_stripped` | blank lines before and after the code inside the fence | code without the blank lines |
| `test_empty_block_skipped` | ```` ```repl\n\n``` ```` | `[]` |
| `test_quotes_inside_code` | code containing `'`, `"` and `'''` | returned unchanged |
| `test_non_greedy_stops_at_first_fence` | two adjacent blocks | two separate entries, not one merged block |
| `test_block_at_end_of_text` | text that ends right after the closing fence | the block is found |

## Run

```bash
pytest -q tests/test_parsing.py
```

## Done when

- [ ] All 10 tests pass, and the step 0 tests still pass.
- [ ] `find_code_blocks` has no side effects and doesn't import anything from
      later steps.

## Pitfalls

* **Greedy `(.*)`.** It merges the first and last blocks into one match,
  swallowing the prose between them. `test_non_greedy_stops_at_first_fence`
  catches this.
* **Forgetting `re.DOTALL`.** Without it, `.` stops at newlines, so only
  one-line blocks match.
* **Writing a fence in a Python test string.** Three backticks are fine
  inside a normal string literal. Build test inputs with
  `"```repl\n" + code + "\n```"` to keep them readable.
