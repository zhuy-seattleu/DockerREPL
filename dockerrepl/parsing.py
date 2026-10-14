"""Find the ```repl code blocks in an LM reply."""

import re
import textwrap

# ```repl, optional spaces/tabs, newline, the code (non-greedy), newline,
# optional indentation, closing ```. DOTALL lets `.` match newlines, so a block
# can span many lines; non-greedy `.*?` stops at the first closing fence.
REPL_BLOCK_PATTERN: re.Pattern[str] = re.compile(r"```repl[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)


def find_code_blocks(text: str) -> list[str]:
    """Return the code of every ```repl block in `text`, in order.

    Each block's common indentation is removed and blank lines at its start
    and end are dropped. Empty blocks, and a final block with no closing
    fence (a truncated reply), are skipped.
    """
    text = text.replace("\r\n", "\n")
    blocks: list[str] = []
    for match in REPL_BLOCK_PATTERN.finditer(text):
        code = textwrap.dedent(match.group(1)).strip("\n")
        if code.strip():
            blocks.append(code)
    return blocks
