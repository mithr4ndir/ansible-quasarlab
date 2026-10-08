"""Two scheduled services share one automation checkout, so the sync must lock.

ansible-proxmox and ansible-security both force-sync
/var/lib/ansible-quasarlab/repo to origin/main. When they overlap, the second
one in loses its whole run:

    fatal: Unable to create '.../.git/index.lock': File exists.
    FATAL: could not pin /var/lib/ansible-quasarlab/repo to origin/main;
           refusing to run.

Observed on 2026-10-07 02:01:42. A cmd_center run deployed the new timer units
and its handler restarted both timers, which fired both services in the same
second. Refusing to run is the sync guard behaving correctly, better than
running from a half-synced tree, but the run is still lost and the message says
nothing about the real cause. The fixed OnCalendar slots make overlap rare, not
impossible: a deploy restarts both timers, and TimeoutStartSec is an hour.

The window is made deterministic rather than raced. A `git` shim on PATH holds
.git/index.lock for a fixed period on `checkout`, then execs the real git, which
is exactly the state a concurrent sync creates. Without the flock the second
caller walks into it; with the flock it waits for the first to finish.

SAFETY: every repo here is a local throwaway under a temp dir. Nothing touches
/var/lib/ansible-quasarlab or any network remote.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib" / "sync-repo.sh"

GIT = "/usr/bin/git" if Path("/usr/bin/git").exists() else "/bin/git"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


class Fixture:
    """A bare origin plus one shared checkout, like the automation checkout."""

    def __init__(self, tmp: str, lock_hold_seconds: float = 0.0):
        self.tmp = Path(tmp)
        self.origin = self.tmp / "origin.git"
        self.checkout = self.tmp / "repo"
        self.bin = self.tmp / "bin"
        self.bin.mkdir(parents=True)

        run([GIT, "init", "--quiet", "--bare", str(self.origin)])
        seed = self.tmp / "seed"
        run([GIT, "clone", "--quiet", str(self.origin), str(seed)])
        (seed / "file.txt").write_text("v1\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        run([GIT, "-C", str(seed), "add", "-A"], env=env)
        run([GIT, "-C", str(seed), "commit", "--quiet", "-m", "seed"], env=env)
        run([GIT, "-C", str(seed), "branch", "-M", "main"], env=env)
        run([GIT, "-C", str(seed), "push", "--quiet", "-u", "origin", "main"], env=env)
        run([GIT, "clone", "--quiet", str(self.origin), str(self.checkout)])

        # git shim: optionally hold index.lock across `checkout`, then real git.
        shim = self.bin / "git"
        shim.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            hold={lock_hold_seconds}
            for a in "$@"; do
              if [ "$a" = "checkout" ] && [ "$hold" != "0.0" ]; then
                # Reproduce exactly what a concurrent git does: own the index.
                d=""
                prev=""
                for x in "$@"; do
                  if [ "$prev" = "-C" ]; then d="$x"; fi
                  prev="$x"
                done
                if [ -n "$d" ]; then
                  : > "$d/.git/index.lock" 2>/dev/null
                  sleep "$hold"
                  rm -f "$d/.git/index.lock"
                fi
                break
              fi
            done
            exec {GIT} "$@"
            """))
        shim.chmod(0o755)

        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}

    def sync(self, extra_env: dict | None = None):
        script = f'source "{LIB}"; sync_repo_to_remote_ref "{self.checkout}" main'
        return subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**self.env, **(extra_env or {})},
        )


class SingleSyncTests(unittest.TestCase):
    def test_a_lone_sync_still_pins_the_checkout(self):
        """The lock must not break the ordinary path."""
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            p = f.sync(); out, err = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, f"sync failed: {err}")
        self.assertIn("pinned to origin/main", out)

    def test_the_lock_file_does_not_land_inside_the_checkout(self):
        """A file inside the tree would be wiped by this function's own
        `clean -fd`, so it has to live beside it."""
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.sync().communicate(timeout=120)
            inside = list(f.checkout.rglob("*.sync.lock"))
            self.assertFalse(inside, f"lock inside the checkout: {inside}")
            self.assertTrue(
                (f.checkout.parent / "repo.sync.lock").exists(),
                "expected the lock beside the checkout",
            )


class ConcurrentSyncTests(unittest.TestCase):
    def test_two_overlapping_syncs_both_succeed(self):
        """The regression. On main the second caller hits index.lock and the
        whole run is refused."""
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp, lock_hold_seconds=3.0)
            first = f.sync()
            # Second starts while the first is holding the index.
            second = f.sync()
            o1, e1 = first.communicate(timeout=120)
            o2, e2 = second.communicate(timeout=120)
        self.assertEqual(first.returncode, 0, f"first sync failed: {e1}")
        self.assertEqual(
            second.returncode, 0,
            "concurrent sync was refused; this is the lost run:\n" + e2,
        )
        self.assertNotIn("index.lock", e2)
        self.assertIn("pinned to origin/main", o2)


class LockTimeoutTests(unittest.TestCase):
    def test_it_gives_up_with_a_clear_message_instead_of_blocking(self):
        """Holding the timer hostage until TimeoutStartSec would be worse than
        failing, and the message must name the real cause."""
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            lock = f.checkout.parent / "repo.sync.lock"
            lock.touch()
            holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "10"])
            try:
                p = f.sync(extra_env={"SYNC_REPO_LOCK_TIMEOUT": "1"})
                out, err = p.communicate(timeout=60)
            finally:
                holder.kill(); holder.wait()
        self.assertNotEqual(p.returncode, 0, "should not claim success")
        self.assertIn("timed out waiting for another run", err)
        self.assertNotIn("pinned to origin/main", out)
