"""WS-B (2026-10-05): the tick and trade runs no longer share a concurrency group, so
they can race on the data-branch push. scripts/push_data_branch.sh is the single
bounded pull-rebase-retry used by BOTH workflows. Exercised against real local git
remotes — no mocks.

Principle 8 note: this is NOT another silent patch — exhaustion exits non-zero
(red job), no `-X theirs`, no swallowing of a rejected push.
"""

import os
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "push_data_branch.sh")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "data", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True,
                   capture_output=True)
    git(seed, "checkout", "-q", "-b", "data")
    (seed / "data.json").write_text("line1\nline2\nline3\n")
    (seed / "other.json").write_text("a\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "data")

    def clone(name):
        d = tmp_path / name
        subprocess.run(["git", "clone", "-q", "-b", "data", str(origin), str(d)], check=True,
                       capture_output=True)
        return d

    return origin, clone


def run_script(cwd, attempts="3"):
    return subprocess.run(["bash", SCRIPT], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "PUSH_MAX_ATTEMPTS": attempts,
                               "PUSH_RETRY_SLEEP": "0", "DATA_BRANCH": "data",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_clean_push(repos):
    origin, clone = repos
    a = clone("a")
    (a / "other.json").write_text("a\nb\n")
    git(a, "commit", "-q", "-am", "tick")
    r = run_script(a)
    assert r.returncode == 0, r.stderr


def test_rejected_push_rebases_and_succeeds_2026_10_05(repos):
    """Tick and trade run both checked out data; the trade run pushed first."""
    origin, clone = repos
    tick, trade = clone("tick"), clone("trade")
    (trade / "data.json").write_text("line1\nline2\nline3\nTRADE\n")
    git(trade, "commit", "-q", "-am", "trade run")
    assert run_script(trade).returncode == 0

    (tick / "other.json").write_text("a\nSNAPSHOT\n")      # disjoint file: clean rebase
    git(tick, "commit", "-q", "-am", "tick")
    r = run_script(tick)
    assert r.returncode == 0, r.stderr
    final = clone("verify")
    assert "TRADE" in (final / "data.json").read_text()
    assert "SNAPSHOT" in (final / "other.json").read_text()


def test_unresolvable_conflict_fails_loudly_and_leaves_clean_repo(repos):
    origin, clone = repos
    tick, trade = clone("tick"), clone("trade")
    (trade / "data.json").write_text("line1\nTRADE\nline3\n")
    git(trade, "commit", "-q", "-am", "trade run")
    assert run_script(trade).returncode == 0

    (tick / "data.json").write_text("line1\nTICK\nline3\n")   # same line: real conflict
    git(tick, "commit", "-q", "-am", "tick")
    r = run_script(tick)
    assert r.returncode != 0                                   # loud, never swallowed
    assert not (tick / ".git" / "rebase-merge").exists()       # no half-applied rebase
    final = clone("verify")
    assert "TRADE" in (final / "data.json").read_text()        # trade data never clobbered
    assert "TICK" not in (final / "data.json").read_text()
