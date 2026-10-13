"""The style checks a grep can be sure about, for one file.

Usage: check-prose.py <file> <register.txt> [<register.local.txt> [<style-patterns.local.txt>]]
       check-prose.py --words <file>...

Prints one line per problem and exits 0 either way. The caller decides.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Scripts and config formats comment with `#`. Everything else here uses `//`
# or a `/* */` block.
HASH_COMMENTS = (".sh", ".bash", ".zsh", ".ps1", ".py", ".rb", ".yml", ".yaml", ".toml", ".tf")

# Data formats have no comment syntax, so every line in one is content.
DATA_FORMATS = (".json", ".lock", ".svg", ".csv", ".snap")

# A string literal on one line. Stripped before looking for a comment marker, so a glob
# such as `"src/**"` does not open a block comment that never closes.
STRINGS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')

AGENT_FILES = ("CLAUDE.md", "AGENTS.md")
STATUS_FILES = AGENT_FILES + ("README.md",)
DEFAULT_BUDGETS = {"claude-budget": 400, "claude-root-budget": 800}
REPORTED_PER_FILE = 8


def prose_lines(path: str, text: str) -> list[tuple[int, str]]:
    """The lines a human wrote as prose, numbered from one."""
    if path.endswith(DATA_FORMATS):
        return []
    out: list[tuple[int, str]] = []
    fenced = False
    in_block = False
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if path.endswith(".md"):
            if stripped.startswith("```"):
                fenced = not fenced
                continue
            if not fenced and stripped and not line.startswith("    "):
                out.append((number, line))
            continue
        if path.endswith(HASH_COMMENTS):
            # A docstring counts as much as a `#` comment does. Tracked by counting
            # fences rather than parsing.
            fences = stripped.count('"""') + stripped.count("'''")
            if in_block or fences:
                out.append((number, line))
                if fences % 2:
                    in_block = not in_block
            elif stripped.startswith("#"):
                out.append((number, line))
            continue
        code = STRINGS.sub("", stripped)
        if "/*" in code:
            in_block = True
        keep = in_block or stripped.startswith("//") or stripped.startswith("*")
        if "*/" in code:
            in_block = False
        if keep:
            out.append((number, line))
    return out


# A number somebody measured, as against a limit the outside world imposes. Keyed on the
# number's neighbour, so a bare constant or a unit on its own never matches.
UNITS = r"ms|s\b|MB|MiB|GB|kB|kbps|Hz|dB|fps"
RECORDED = re.compile(
    rf"~\s*[\d.]+\s*({UNITS})"
    rf"|\bmeasured\s+(at|around|about)?\s*~?\s*[\d.]+\s*({UNITS})"
    rf"|\b(costs?|takes?|spends?)\s+(about|around|roughly|~)?\s*[\d.]+\s*({UNITS})"
    rf"|\b(roughly|about|around)\s+[\d.]+\s*({UNITS})",
    re.IGNORECASE,
)

HISTORY = re.compile(
    r"measured on [0-9]|\b(during|after|in) a review pass|\ban earlier version|\b(this|it) used to\b"
    r"|(?<!is )(?<!are )(?<!was )(?<!be )\bused to (read|be)\b|\bthe first version\b|\blearned the hard way|\bwe moved to a\b",
    re.IGNORECASE,
)

STATUS_WORDS = r"(history|background|decisions?|state|status|next|next steps|roadmap|changelog)"
STATUS_HEADING = re.compile(rf"^\s*(#{{1,6}}\s+{STATUS_WORDS}\s*$|\*\*{STATUS_WORDS}\*\*)", re.IGNORECASE)
STATUS = re.compile(
    r"\bis now done\b|\bnot yet (built|implemented|done|supported|wired|shipped|written|finished|ported)\b|\bfor now\b|\bwas (added|removed|replaced|changed|moved)\b"
    r"|\bwe (decided|chose|agreed)\b|\b(bundle|spec) #?\d{3}\b|\b_(todo|done)/|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

# Each is (name, pattern, message). A name can be switched off per repository with a
# `-name` line in the local register file.
BUILTINS: list[tuple[str, re.Pattern[str], str]] = [
    (
        # A dash alone in a table cell is a placeholder for "none", not a joint. A dialogue
        # dash touches the word on one side only and is not one either.
        "emdash",
        re.compile(r"(?<!\|)(?<!\|\s)(?:(?<=\S)—(?=\S)|(?<=\s)—(?=\s))(?!\s*\|)|(?<=\s)–(?=\s)"),
        "uses a dash as a joint. Comma for an aside, colon before a definition, "
        "full stop when it is its own thought.",
    ),
    ("curly", re.compile(r"[‘’“”]"), "uses curly quotes. Straight quotes only."),
    ("emoji", re.compile(r"[\U0001f300-\U0001faff✅❌⚠✨❗❤]"), "has an emoji in technical prose."),
    ("measurement", RECORDED, "reads as a recorded measurement. Keep the constraint, drop the number."),
    ("history", HISTORY, "records history. Git holds it; state only what is true now."),
]

STATUS_MESSAGE = "reads as status, roadmap or a decision record. Describe the code as it is now."


@dataclass(frozen=True)
class Rules:
    literals: tuple[tuple[str, re.Pattern[str]], ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    disabled: frozenset[str] = frozenset()
    budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    deny: re.Pattern[str] | None = None
    allow: re.Pattern[str] | None = None

    def allows(self, line: str) -> bool:
        return bool(self.allow and self.allow.search(line))


def as_phrase(phrase: str) -> re.Pattern[str]:
    """A literal, anchored at both ends so it cannot match inside a longer word."""
    body = re.escape(phrase)
    lead = r"\b" if phrase[:1].isalnum() else ""
    tail = r"\b" if phrase[-1:].isalnum() else ""
    return re.compile(lead + body + tail, re.IGNORECASE)


def read_lines(path: str) -> list[str]:
    try:
        return [line.strip() for line in open(path, encoding="utf-8").read().split("\n")]
    except OSError:
        return []


def compile_any(bodies: list[str], flags: int = 0) -> re.Pattern[str] | None:
    """One pattern matching any of the bodies, skipping a body that does not compile."""
    valid = []
    for body in bodies:
        try:
            re.compile(body)
        except re.error:
            continue
        valid.append(f"(?:{body})")
    return re.compile("|".join(valid), flags) if valid else None


def after(prefix: str, lines: list[str]) -> list[str]:
    return [line[len(prefix):].strip() for line in lines if line.startswith(prefix) and line[len(prefix):].strip()]


def load_rules(registers: list[str], patterns_file: str | None = None) -> Rules:
    """Register lines are `!phrase`, `~regex`, `-builtin` or `=setting value`.

    The patterns file holds extended regular expressions for identity detail, and a
    `-regex` line in it lets every check pass on a line that matches.
    """
    lines = [line for path in registers for line in read_lines(path)]
    settings = dict(DEFAULT_BUDGETS)
    for setting in after("=", lines):
        name, _, value = setting.partition(" ")
        if name in settings and value.strip().isdigit():
            settings[name] = int(value)
    identity = [line for line in read_lines(patterns_file or "") if line and not line.startswith("#")]
    return Rules(
        literals=tuple((phrase.lower(), as_phrase(phrase)) for phrase in after("!", lines)),
        patterns=tuple(filter(None, (compile_any([body], re.IGNORECASE) for body in after("~", lines)))),
        disabled=frozenset(after("-", lines)),
        budgets=settings,
        deny=compile_any([line for line in identity if not line.startswith("-")]),
        allow=compile_any(after("-", identity)),
    )


def hardwrapped(text: str) -> bool:
    """Three prose lines in a row, each near 80 columns."""
    fenced, run = False, 0
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced, run = not fenced, 0
            continue
        prose = not fenced and line.strip() and not re.match(r"^\s*([|>#\-*+]|\d+\.|\s{4})", line)
        run = run + 1 if prose and 60 <= len(line) <= 92 else 0
        if run >= 3:
            return True
    return False


def line_problem(line: str, rules: Rules, builtins: list[tuple[str, re.Pattern[str], str]], status: bool) -> str | None:
    hit = next((phrase for phrase, pattern in rules.literals if pattern.search(line)), None)
    if hit:
        return f'"{hit}" reads as an assistant. See the register list.'
    if any(pattern.search(line) for pattern in rules.patterns):
        return "reads as an assistant construction. See voice/constructions.md."
    message = next((message for _, pattern, message in builtins if pattern.search(line)), None)
    if message:
        return message
    if status and (STATUS_HEADING.search(line) or STATUS.search(line)):
        return STATUS_MESSAGE
    return None


def find_problems(path: str, text: str, rules: Rules) -> list[str]:
    """Every problem in the text, with nothing capped and nothing read from disk."""
    problems: list[str] = []
    if rules.deny:
        numbers = [n for n, line in enumerate(text.split("\n"), 1) if rules.deny.search(line) and not rules.allows(line)]
        if numbers:
            problems.append(f"line {numbers[0]}: identity or infrastructure detail. Use a placeholder.")
    if path.endswith(".md") and "hardwrap" not in rules.disabled and hardwrapped(text):
        problems.append("looks hard-wrapped near 80 columns. One paragraph is one line; editors wrap.")
    builtins = [check for check in BUILTINS if check[0] not in rules.disabled]
    status = os.path.basename(path) in STATUS_FILES
    for number, line in prose_lines(path, text):
        problem = None if rules.allows(line) else line_problem(line, rules, builtins, status)
        if problem:
            problems.append(f"line {number}: {problem}")
    return problems


def prose_words(path: str, text: str) -> int:
    return sum(len(re.findall(r"\w[\w'-]*", line)) for _, line in prose_lines(path, text))


def git(directory: str, *arguments: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", directory, *arguments], capture_output=True, text=True)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def budget_for(path: str, rules: Rules) -> tuple[str, int] | None:
    """The setting and word budget for an agent file: the root one, or a nested one."""
    if os.path.basename(path) not in AGENT_FILES:
        return None
    directory = os.path.dirname(os.path.realpath(path))
    top = (git(directory, "rev-parse", "--show-toplevel") or os.environ.get("CLAUDE_PROJECT_DIR", "")).strip()
    name = "claude-root-budget" if top and os.path.realpath(top) == directory else "claude-budget"
    return name, rules.budgets[name]


def budget_problem(path: str, text: str, rules: Rules) -> str | None:
    """An agent file over its budget that grew since HEAD. An untracked file grew from nothing."""
    budget = budget_for(path, rules)
    words = prose_words(path, text)
    if budget is None or words <= budget[1]:
        return None
    committed = git(os.path.dirname(os.path.realpath(path)), "show", f"HEAD:./{os.path.basename(path)}")
    before = prose_words(path, committed) if committed is not None else 0
    if words <= before:
        return None
    name, limit = budget
    return (
        f"{words} prose words against a budget of {limit}, and {before} at HEAD. Cut it to what an agent "
        f"cannot read off the code, or set `={name} <words>` in .claude/register.local.txt."
    )


def print_word_counts(paths: list[str]) -> None:
    """`--words`: each file's prose words, and its budget where it has one."""
    top = (git(os.getcwd(), "rev-parse", "--show-toplevel") or os.getcwd()).strip()
    rules = load_rules([os.path.join(top, ".claude", "register.local.txt")])
    for path in paths:
        budget = budget_for(path, rules)
        words = prose_words(path, open(path, encoding="utf-8", errors="replace").read())
        print(f"{words:6} {budget[1] if budget else '-':>6}  {path}")


def main() -> int:
    if sys.argv[1] == "--words":
        print_word_counts(sys.argv[2:])
        return 0
    path, registers = sys.argv[1], sys.argv[2:4]
    patterns_file = sys.argv[4] if len(sys.argv) > 4 else None
    raw = open(path, "rb").read()
    if b"\x00" in raw[:8192]:
        return 0
    text = raw.decode("utf-8", errors="replace")
    rules = load_rules(registers, patterns_file)
    problems = find_problems(path, text, rules)
    over = budget_problem(path, text, rules)
    for problem in ([over] if over else []) + problems[:REPORTED_PER_FILE]:
        print(problem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
