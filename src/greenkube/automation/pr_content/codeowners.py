"""CODEOWNERS parsing and path matching (roadmap J3).

Three dialects are supported:

* ``github``: gitignore-style patterns, the last matching line wins.
* ``gitlab``: the GitHub syntax plus ``[Section]`` / ``^[Optional section]``
  headers with optional default owners; the last match of every section
  applies and the owners of all sections are combined.
* ``gitea``: Go regular expressions anchored to the whole path, optionally
  negated with ``!``; every matching rule adds its owners.

Owners are returned as written (``@user``, ``@org/team``, ``user@example.com``);
mapping them to provider reviewers is the adapter's job because GitLab needs
user IDs and GitHub only accepts teams of the repository's organization.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

Dialect = Literal["github", "gitlab", "gitea"]

#: Where each provider looks for the file, in its documented precedence order.
CODEOWNERS_PATHS: dict[str, tuple[str, ...]] = {
    "github": (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"),
    "gitlab": ("CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"),
    "gitea": ("CODEOWNERS", "docs/CODEOWNERS", ".gitea/CODEOWNERS"),
}
MAX_CODEOWNERS_BYTES = 256 * 1024
MAX_RULES = 2_000
_SECTION = re.compile(r"^\^?\[(?P<name>[^\]]+)\](?:\[\d+\])?(?P<owners>.*)$")


@dataclass(frozen=True, slots=True)
class CodeOwnersRule:
    pattern: str
    owners: tuple[str, ...]
    section: str = ""
    negated: bool = False
    regex: re.Pattern[str] | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class CodeOwners:
    dialect: Dialect
    rules: tuple[CodeOwnersRule, ...] = ()
    #: Lines that could not be parsed (reported, never fatal).
    errors: tuple[str, ...] = ()

    def owners_for(self, paths: Iterable[str]) -> list[str]:
        """Owners of every path, deduplicated in first-seen order."""
        owners: list[str] = []
        for path in paths:
            for owner in self._owners_of(path.lstrip("/")):
                if owner not in owners:
                    owners.append(owner)
        return owners

    def _owners_of(self, path: str) -> tuple[str, ...]:
        if self.dialect == "gitea":
            collected: list[str] = []
            for rule in self.rules:
                assert rule.regex is not None
                if bool(rule.regex.fullmatch(path)) != rule.negated:
                    collected.extend(owner for owner in rule.owners if owner not in collected)
            return tuple(collected)
        # Last match wins, per section (GitHub has a single unnamed section).
        last: dict[str, CodeOwnersRule] = {}
        for rule in self.rules:
            assert rule.regex is not None
            if rule.regex.fullmatch(path):
                last[rule.section] = rule
        combined: list[str] = []
        for rule in last.values():
            combined.extend(owner for owner in rule.owners if owner not in combined)
        return tuple(combined)


def parse_codeowners(content: str, dialect: Dialect = "github") -> CodeOwners:
    """Parse a CODEOWNERS file; malformed lines are skipped and reported."""
    if len(content.encode("utf-8", errors="replace")) > MAX_CODEOWNERS_BYTES:
        return CodeOwners(dialect=dialect, errors=("CODEOWNERS file is larger than the supported 256 KiB.",))
    rules: list[CodeOwnersRule] = []
    errors: list[str] = []
    section = ""
    section_defaults: tuple[str, ...] = ()
    for number, raw_line in enumerate(content.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if dialect == "gitlab" and (line.startswith("[") or line.startswith("^[")):
            match = _SECTION.match(line)
            if match is None:
                errors.append(f"line {number}: invalid section header")
                continue
            section = match.group("name").strip().casefold()
            section_defaults = tuple(_owner_tokens(match.group("owners").split()))
            continue
        tokens = _split_tokens(line)
        pattern, owner_tokens = tokens[0], tokens[1:]
        owners = tuple(_owner_tokens(owner_tokens)) or (section_defaults if dialect == "gitlab" else ())
        negated = False
        if dialect == "gitea" and pattern.startswith("!"):
            negated, pattern = True, pattern[1:]
        try:
            regex = _gitea_regex(pattern) if dialect == "gitea" else _glob_regex(pattern)
        except re.error:
            errors.append(f"line {number}: invalid pattern {pattern!r}")
            continue
        rules.append(CodeOwnersRule(pattern=pattern, owners=owners, section=section, negated=negated, regex=regex))
        if len(rules) >= MAX_RULES:
            errors.append(f"only the first {MAX_RULES} rules are used")
            break
    return CodeOwners(dialect=dialect, rules=tuple(rules), errors=tuple(errors))


def _strip_comment(line: str) -> str:
    """Drop a ``#`` comment unless it is escaped (``\\#``)."""
    result: list[str] = []
    escaped = False
    for character in line:
        if character == "#" and not escaped:
            break
        escaped = character == "\\" and not escaped
        result.append(character)
    return "".join(result)


def _split_tokens(line: str) -> list[str]:
    """Split on unescaped whitespace (``\\ `` keeps a space inside a pattern)."""
    tokens: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(character)
    if current:
        tokens.append("".join(current))
    return tokens


def _owner_tokens(tokens: Sequence[str]) -> list[str]:
    owners: list[str] = []
    for token in tokens:
        token = token.strip()
        # ``@@role`` (GitLab roles) cannot be requested as reviewers.
        if not token or token.startswith("@@"):
            continue
        if token.startswith("@") or ("@" in token and "." in token.split("@", 1)[1]):
            if token not in owners:
                owners.append(token)
    return owners


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style CODEOWNERS pattern to a full-path regex."""
    directory_only = pattern.endswith("/")
    body = pattern.strip("/") if pattern != "/" else ""
    anchored = pattern.startswith("/") or "/" in body
    if body in ("", "*", "**"):
        return re.compile(r".*")
    translated = _translate_glob(body)
    prefix = "" if anchored else r"(?:.*/)?"
    # A pattern names a file or a directory: a directory owns everything below it,
    # except ``dir/*``, which only matches the entries directly inside ``dir``.
    if directory_only:
        suffix = r"/.*"
    elif body.endswith("/*") and not body.endswith("**/*"):
        suffix = ""
    else:
        suffix = r"(?:/.*)?"
    return re.compile(prefix + translated + suffix)


def _translate_glob(glob: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        character = glob[index]
        if glob.startswith("**/", index):
            parts.append(r"(?:.*/)?")
            index += 3
        elif glob.startswith("/**", index) and index + 3 == len(glob):
            parts.append(r"/.*")
            index += 3
        elif glob.startswith("**", index):
            parts.append(r".*")
            index += 2
        elif character == "*":
            parts.append(r"[^/]*")
            index += 1
        elif character == "?":
            parts.append(r"[^/]")
            index += 1
        elif character == "[":
            end = glob.find("]", index + 1)
            if end == -1:
                parts.append(re.escape(character))
                index += 1
            else:
                content = glob[index + 1 : end].replace("\\", "\\\\")
                if content.startswith("!"):
                    content = "^" + content[1:]
                parts.append(f"[{content}]")
                index = end + 1
        else:
            parts.append(re.escape(character))
            index += 1
    return "".join(parts)


def _gitea_regex(pattern: str) -> re.Pattern[str]:
    # Go RE2 and Python agree on the subset used in CODEOWNERS files. Python's
    # engine backtracks where RE2 does not, so constructs that can backtrack
    # exponentially on a path (and RE2-only-invalid ones) are rejected.
    pattern = pattern.lstrip("/")
    if len(pattern) > MAX_GITEA_PATTERN_CHARS:
        raise re.error(f"pattern longer than {MAX_GITEA_PATTERN_CHARS} characters")
    _reject_backtracking_constructs(pattern)
    return re.compile(pattern)


#: Longest Gitea CODEOWNERS regular expression GreenKube compiles.
MAX_GITEA_PATTERN_CHARS = 512


def _reject_backtracking_constructs(pattern: str) -> None:
    """Raise :class:`re.error` for a repeated group that itself repeats or alternates, backrefs and lookarounds.

    ``(a+)+``, ``(a|aa)*`` or ``(x*){2,}`` make a backtracking engine take
    exponential time on a non-matching path; RE2, which Gitea uses, rejects
    backreferences and lookarounds, so no valid Gitea rule needs them.
    """
    # Per open group: does its body contain a repetition or an alternation?
    stack: list[bool] = []
    index, length = 0, len(pattern)
    while index < length:
        character = pattern[index]
        if character == "\\":
            if index + 1 < length and pattern[index + 1].isdigit() and pattern[index + 1] != "0":
                raise re.error("backreferences are not supported")
            index += 2
            continue
        if character == "[":
            index = _skip_class(pattern, index)
            continue
        if character == "(":
            if pattern.startswith(("(?=", "(?!", "(?<=", "(?<!"), index):
                raise re.error("lookarounds are not supported")
            stack.append(False)
        elif character == ")":
            risky = stack.pop() if stack else False
            if risky and _repeats(pattern, index + 1):
                raise re.error("a repeated group must not itself repeat or alternate")
            if stack and (risky or _repeats(pattern, index + 1)):
                stack[-1] = True
        elif character == "|" and stack:
            stack[-1] = True
        elif character in "*+{" and stack and _repeats(pattern, index):
            stack[-1] = True
        index += 1


def _repeats(pattern: str, index: int) -> bool:
    """Whether a quantifier allowing more than one repetition starts at ``index``."""
    if index >= len(pattern):
        return False
    if pattern[index] in "*+":
        return True
    match = re.match(r"\{(\d*)(,?)(\d*)\}", pattern[index:])
    if match is None:
        return False
    low, comma, high = match.groups()
    return bool(comma) and high != "1" or (not comma and low not in ("", "0", "1"))


def _skip_class(pattern: str, index: int) -> int:
    """Index just after the character class that starts at ``index``."""
    index += 1
    if index < len(pattern) and pattern[index] == "^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern):
        if pattern[index] == "\\":
            index += 2
            continue
        if pattern[index] == "]":
            return index + 1
        index += 1
    return index
