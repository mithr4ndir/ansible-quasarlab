"""The documented test surfaces in AGENTS.md have to match the ones on disk.

Two different ways that list has already gone wrong, both silent:

1. `roles/common/apps/kubectl/tests` is four levels down and was missing from an
   earlier version of the list. AGENTS.md says so itself.
2. #230 added `roles/pbs/tests` to the list and left the sentence above it saying
   nine, so the prose and the block under it disagreed.

Neither breaks a test run, which is the problem: the list is how a human decides
what to run, and a surface nobody runs is a surface that can rot green. This test
is the only thing that fails when the two drift apart.

The second failure is now designed out rather than asserted against: the sentence
no longer states a number, so adding a surface needs one new line in the block and
nothing else. A count in prose next to the thing it counts is a conflict waiting
for two branches that both add a test directory.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "AGENTS.md"
TEXT = AGENTS.read_text()


def documented_surfaces() -> list[str]:
    """The `pytest <path>` lines in the first fenced block after the claim."""
    anchor = TEXT.index("pytest surfaces below are separate")
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


def test_the_list_is_not_empty():
    # The two tests above both pass vacuously against a block that parsed as
    # nothing, which is exactly what a reworded heading would produce.
    assert len(documented_surfaces()) >= 9, (
        "parsed no surface list out of AGENTS.md, so the two tests above are "
        "asserting over an empty set"
    )
