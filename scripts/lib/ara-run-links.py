#!/usr/bin/env python3
"""Print ansible_playbook_last_run_info lines linking playbooks to their ARA run.

Usage:
    ara-run-links.py --base-url URL --label LABEL [--timeout SECS] PLAYBOOK...

The wrappers tag every ansible-playbook they start with one ARA label that is
unique to that wrapper run (ARA_DEFAULT_LABELS, see ara-run-links.sh). This
asks the ARA API for the playbooks carrying that label and prints one info
series per requested playbook it finds:

    ansible_playbook_last_run_info{playbook="vm_baseline.yml",ara_url="http://.../playbooks/13685.html"} 1

Correlating by label rather than "newest playbook with this name" is what makes
the link right when a manual run of the same playbook overlaps a timer run.

Exit status: 0 with the lines (possibly none) on stdout, or 1 with a reason on
stderr. Output is written in one go at the end, so a caller that only uses
stdout on exit 0 never sees half a result. Callers must treat any failure as
"no link": a missing link is never a reason to lose a metric.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

# A run is at most a handful of playbooks, each ~1.6 KB of JSON in ARA 1.7.2.
PAGE_LIMIT = 100
MAX_RESPONSE_BYTES = 1024 * 1024

# SECURITY: every value below ends up inside a Prometheus label, so anything
# that could carry a quote, backslash or newline into the exposition format is
# rejected rather than escaped.
BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,254}$")
PLAYBOOK_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")

HELP = (
    "# HELP ansible_playbook_last_run_info ARA report of the last run of each playbook (always 1).\n"
    "# TYPE ansible_playbook_last_run_info gauge\n"
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("playbooks", nargs="+")
    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")
    if not BASE_URL_RE.match(args.base_url):
        parser.error("invalid --base-url")
    if not LABEL_RE.match(args.label):
        parser.error("invalid --label")
    for name in args.playbooks:
        if not PLAYBOOK_RE.match(name):
            parser.error(f"invalid playbook name: {name!r}")
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be in (0, 60]")
    return args


def fetch_labelled_playbooks(base_url: str, label: str, timeout: float) -> list:
    query = urllib.parse.urlencode({"label": label, "limit": PAGE_LIMIT})
    # No proxies: the API is on the LAN, and an http_proxy in the environment
    # must not route (or stall) this call.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"{base_url}/api/v1/playbooks?{query}", headers={"Accept": "application/json"}
    )
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("ARA response too large")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("ARA response has no results list")
    count = payload.get("count")
    if isinstance(count, int) and count > len(payload["results"]):
        # More playbooks under one run label than a run can hold: the label is
        # not unique, so nothing it returns can be trusted to be this run.
        raise ValueError(f"label {label} matched {count} playbooks")
    return payload["results"]


def ids_by_playbook(results: list, wanted: set[str]) -> dict[str, int]:
    """Map playbook file name to ARA id. A name seen twice is dropped."""
    found: dict[str, int] = {}
    ambiguous: set[str] = set()
    for record in results:
        if not isinstance(record, dict):
            continue
        playbook_id = record.get("id")
        path = record.get("path")
        # bool is an int subclass; True must not become playbook 1.
        if isinstance(playbook_id, bool) or not isinstance(playbook_id, int) or playbook_id <= 0:
            continue
        if not isinstance(path, str):
            continue
        # ARA's own path filter is a substring match; compare exact file names.
        name = PurePosixPath(path).name
        if name not in wanted:
            continue
        if name in found and found[name] != playbook_id:
            ambiguous.add(name)
        found[name] = playbook_id
    for name in ambiguous:
        print(f"ara-run-links: {name} appears more than once under one run label; not linking it", file=sys.stderr)
        del found[name]
    return found


def render(base_url: str, playbooks: list[str], ids: dict[str, int]) -> str:
    lines = [
        f'ansible_playbook_last_run_info{{playbook="{name}",ara_url="{base_url}/playbooks/{ids[name]}.html"}} 1\n'
        for name in playbooks
        if name in ids
    ]
    return HELP + "".join(lines) if lines else ""


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        results = fetch_labelled_playbooks(args.base_url, args.label, args.timeout)
    except (OSError, ValueError) as exc:
        # OSError covers URLError, HTTPError, refused connections and socket
        # timeouts; ValueError covers bad JSON and a non-unique label.
        print(f"ara-run-links: lookup failed: {exc}", file=sys.stderr)
        return 1
    ids = ids_by_playbook(results, set(args.playbooks))
    missing = [name for name in args.playbooks if name not in ids]
    if missing:
        print(f"ara-run-links: no ARA record under {args.label} for: {' '.join(missing)}", file=sys.stderr)
    sys.stdout.write(render(args.base_url, args.playbooks, ids))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
