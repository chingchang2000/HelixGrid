#!/usr/bin/env python3
"""Deterministic source-code statistics for the HelixGrid monorepo.

Unlike GitHub's language bar, this script is intentionally transparent about what it counts.
It scans hand-maintained source/configuration languages, ignores generated/build/vendor trees,
and can enforce a minimum physical source-line target in CI.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Iterator


LANGUAGES = {
    ".go": "Go",
    ".rs": "Rust",
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".c": "C",
    ".h": "C/C++ Header",
    ".hpp": "C/C++ Header",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".cs": "C#",
    ".sql": "SQL",
    ".proto": "Protocol Buffers",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".lua": "Lua",
    ".rb": "Ruby",
    ".swift": "Swift",
}

IGNORED_DIRS = {
    ".git",
    ".github",
    ".build",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "coverage",
    "vendor",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

GENERATED_MARKERS = (
    "code generated",
    "@generated",
    "generated file",
    "do not edit",
)


@dataclass(slots=True)
class FileStats:
    path: str
    language: str
    physical: int
    blank: int
    comment_only: int
    code_like: int
    bytes: int


@dataclass(slots=True)
class LanguageStats:
    language: str
    files: int = 0
    physical: int = 0
    blank: int = 0
    comment_only: int = 0
    code_like: int = 0
    bytes: int = 0

    def add(self, item: FileStats) -> None:
        self.files += 1
        self.physical += item.physical
        self.blank += item.blank
        self.comment_only += item.comment_only
        self.code_like += item.code_like
        self.bytes += item.bytes


@dataclass(slots=True)
class Totals:
    files: int = 0
    physical: int = 0
    blank: int = 0
    comment_only: int = 0
    code_like: int = 0
    bytes: int = 0

    def add(self, item: FileStats) -> None:
        self.files += 1
        self.physical += item.physical
        self.blank += item.blank
        self.comment_only += item.comment_only
        self.code_like += item.code_like
        self.bytes += item.bytes


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Count hand-maintained HelixGrid source lines")
    result.add_argument("root", nargs="?", default=".", help="repository root")
    result.add_argument("--minimum", type=int, default=0, help="fail when physical source lines are below N")
    result.add_argument("--minimum-code", type=int, default=0, help="fail when nonblank/non-comment lines are below N")
    result.add_argument("--json", action="store_true", help="emit JSON")
    result.add_argument("--files", action="store_true", help="include per-file rows")
    result.add_argument("--include-generated", action="store_true", help="include files marked as generated")
    return result


def discover(root: pathlib.Path) -> Iterator[pathlib.Path]:
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        base = pathlib.Path(directory)
        for name in sorted(files):
            path = base / name
            if path.suffix.lower() in LANGUAGES:
                yield path


def is_generated(text: str) -> bool:
    head = text[:4096].lower()
    return any(marker in head for marker in GENERATED_MARKERS)


def comment_prefixes(language: str) -> tuple[str, ...]:
    if language in {"Python", "Shell", "PowerShell", "Ruby"}:
        return ("#",)
    if language == "SQL":
        return ("--",)
    if language == "Lua":
        return ("--",)
    return ("//",)


def analyze_file(root: pathlib.Path, path: pathlib.Path, include_generated: bool) -> FileStats | None:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"warning: skipping {path}: {exc}", file=sys.stderr)
        return None
    if not include_generated and is_generated(text):
        return None

    language = LANGUAGES[path.suffix.lower()]
    prefixes = comment_prefixes(language)
    physical = blank = comment_only = code_like = 0
    in_block_comment = False

    for line in text.splitlines():
        physical += 1
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue

        # This is deliberately a conservative lexical approximation, not a parser.
        # It is sufficient for a reproducible "code-like" metric while physical lines
        # remain the canonical target used by the repository size gate.
        if in_block_comment:
            comment_only += 1
            if "*/" in stripped:
                in_block_comment = False
                tail = stripped.split("*/", 1)[1].strip()
                if tail:
                    comment_only -= 1
                    code_like += 1
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block_comment = True
            if stripped.endswith("*/") or in_block_comment:
                comment_only += 1
                continue
        if any(stripped.startswith(prefix) for prefix in prefixes):
            comment_only += 1
            continue
        code_like += 1

    # splitlines() does not count a final empty line, which is desirable: physical lines
    # here means actual textual records containing either content or interior whitespace.
    relative = path.relative_to(root).as_posix()
    return FileStats(
        path=relative,
        language=language,
        physical=physical,
        blank=blank,
        comment_only=comment_only,
        code_like=code_like,
        bytes=len(raw),
    )


def collect(root: pathlib.Path, include_generated: bool) -> tuple[list[FileStats], dict[str, LanguageStats], Totals]:
    files: list[FileStats] = []
    languages: dict[str, LanguageStats] = {}
    totals = Totals()
    for path in discover(root):
        item = analyze_file(root, path, include_generated)
        if item is None:
            continue
        files.append(item)
        totals.add(item)
        stats = languages.setdefault(item.language, LanguageStats(language=item.language))
        stats.add(item)
    files.sort(key=lambda item: item.path)
    return files, dict(sorted(languages.items())), totals


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    raise AssertionError("unreachable")


def print_human(files: list[FileStats], languages: dict[str, LanguageStats], totals: Totals, show_files: bool) -> None:
    print("HelixGrid source statistics")
    print("=" * 79)
    print(f"{'Language':20} {'Files':>7} {'Physical':>10} {'Code-like':>10} {'Comments':>10} {'Blank':>8} {'Bytes':>10}")
    print("-" * 79)
    for stats in sorted(languages.values(), key=lambda item: (-item.physical, item.language)):
        print(
            f"{stats.language:20} {stats.files:7d} {stats.physical:10d} {stats.code_like:10d} "
            f"{stats.comment_only:10d} {stats.blank:8d} {format_bytes(stats.bytes):>10}"
        )
    print("-" * 79)
    print(
        f"{'TOTAL':20} {totals.files:7d} {totals.physical:10d} {totals.code_like:10d} "
        f"{totals.comment_only:10d} {totals.blank:8d} {format_bytes(totals.bytes):>10}"
    )
    if show_files:
        print()
        print("Files")
        print("-" * 79)
        for item in files:
            print(f"{item.physical:7d}  {item.language:18}  {item.path}")


def json_payload(files: list[FileStats], languages: dict[str, LanguageStats], totals: Totals, show_files: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "totals": dataclasses.asdict(totals),
        "languages": [dataclasses.asdict(item) for item in sorted(languages.values(), key=lambda item: (-item.physical, item.language))],
    }
    if show_files:
        payload["files"] = [dataclasses.asdict(item) for item in files]
    return payload


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(list(argv) if argv is not None else None)
    root = pathlib.Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    files, languages, totals = collect(root, args.include_generated)
    if args.json:
        print(json.dumps(json_payload(files, languages, totals, args.files), indent=2, sort_keys=True))
    else:
        print_human(files, languages, totals, args.files)

    failed = False
    if args.minimum and totals.physical < args.minimum:
        print(
            f"error: source-size gate failed: {totals.physical} physical lines < required {args.minimum}",
            file=sys.stderr,
        )
        failed = True
    if args.minimum_code and totals.code_like < args.minimum_code:
        print(
            f"error: code-like gate failed: {totals.code_like} lines < required {args.minimum_code}",
            file=sys.stderr,
        )
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
