"""The documented test surfaces in AGENTS.md have to match the ones on disk.

Two different ways that list has already gone wrong, both silent:

1. `roles/common/apps/kubectl/tests` is four levels down and was missing from an
   earlier version of the list. AGENTS.md says so itself.
2. #230 added `roles/pbs/tests` to the list and left the prose count at nine, so
   the sentence and the block under it disagreed.

Neither breaks a test run, which is the problem: the list is how a human decides
what to run, and a surface nobody runs is a surface that can rot green. This
test is the only thing that fails when the two drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "AGENTS.md"
TEXT = AGENTS.read_text()

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}


def documented_surfaces() -> list[str]:
    """The `pytest <path>` lines in the first fenced block after the claim."""
    anchor = TEXT.index("separate pytest surfaces exist")
    block = TEXT.index("```", anchor)
    end = TEXT.index("```", block + 3)
    return re.findall(r"^pytest (\S+)", TEXT[block:end], re.MULTILINE)


def actual_surfaces() -> set[str]:
    found = {"tests/"}
    for d in REPO.glob("roles/**/tests"):
        if d.is_dir():
            found.add(d.relative_to(REPO).as_posix())
    return found


def test_every_test_directory_on_disk_is_in_the_documented_list():
    documented = {s.rstrip("/") for s in documented_surfaces()}
    missing = sorted(s for s in actual_surfaces() if s.rstrip("/") not in documented)
    assert not missing, (
        "these test directories exist but are not in the AGENTS.md list, so "
        f"nobody runs them by habit: {missing}"
    )


def test_every_documented_surface_exists():
    stale = sorted(s for s in documented_surfaces() if not (REPO / s).is_dir())
    assert not stale, f"AGENTS.md lists test paths that are gone: {stale}"


def test_the_prose_count_matches_the_block():
    match = re.search(r"\b(\w+) separate pytest surfaces exist", TEXT)
    assert match, "the sentence introducing the surface list has changed shape"
    word = match.group(1).lower()
    assert word in WORDS, f"unexpected count word {match.group(1)!r} in AGENTS.md"
    assert WORDS[word] == len(documented_surfaces()), (
        f"AGENTS.md says {word} surfaces but lists {len(documented_surfaces())}"
    )
