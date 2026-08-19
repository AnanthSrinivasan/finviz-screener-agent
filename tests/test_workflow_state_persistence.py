"""Every state file an agent writes must be committed by the workflow that
runs it — otherwise the write happens in the runner and is silently discarded.

Two live instances of this, both found 2026-08-19:

  - data/paper_trading_state.json was in NO workflow's git add list. The paper
    monitor and executor rewrote it every run all summer; each run reverted to
    the 2026-05-03 commit. Paper sizing_mode was pinned at "normal" for 3.5
    months and the 3-consecutive-losses suspension could never fire.
  - data/recent_events.json was missing from market_monitor.yml, the workflow
    running its only writer. The events feed stayed frozen at 2026-04-27 even
    after the file itself was repaired.

Neither is detectable from Python alone — the code is correct in both cases.
"""
import glob
import os
import re
import unittest

WORKFLOW_DIR = ".github/workflows"

# module path -> state files it persists
AGENT_WRITES = {
    "agents/trading/alpaca_monitor.py": [
        "data/paper_stops.json",
        "data/paper_trading_state.json",
    ],
    "agents/trading/alpaca_executor.py": [
        "data/paper_stops.json",
        "data/paper_trading_state.json",
    ],
    "agents/market/market_monitor.py": [
        "data/market_monitor_history.json",
        "data/trading_state.json",
        "data/recent_events.json",
    ],
    "agents/trading/position_monitor.py": [
        "data/positions.json",
        "data/trading_state.json",
        "data/position_history.json",
        "data/book_last_post.json",
    ],
}

# Files only the live profile writes — asserted only where TRADING_PROFILE=live.
LIVE_WRITES = ["data/live_alpaca_stops.json", "data/live_alpaca_trading_state.json"]


def _module_forms(path: str):
    """A workflow may invoke an agent as a path or as a -m module."""
    return {path, path.replace("/", ".").replace(".py", "")}


def _committed_paths(body: str) -> str:
    """All text a workflow feeds to `git add`, including the
    `for f in <files>; do git add "$f"; done` loop form."""
    chunks = re.findall(r"git add ([^\n|&]+)", body)
    chunks += re.findall(r"for f in ([^\n;]+)", body)
    return " ".join(chunks)


class WorkflowStatePersistenceTests(unittest.TestCase):
    def test_every_written_state_file_is_committed(self):
        """Profile is per-step, not per-workflow: position-critical.yml runs the
        monitor ONLY as TRADING_PROFILE=live, so it legitimately never touches
        the paper state files."""
        problems = []
        for wf in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml"))):
            body = open(wf).read()
            added = _committed_paths(body)
            if re.search(r"git add\s+data/\s", added + " ") or "git add data/ " in body:
                continue  # commits the whole data dir
            for step in re.split(r"\n\s+- name:", body):
                is_live = "TRADING_PROFILE: live" in step
                for module, files in AGENT_WRITES.items():
                    if not any(form in step for form in _module_forms(module)):
                        continue
                    expected = LIVE_WRITES if is_live else list(files)
                    for state_file in expected:
                        if state_file not in added:
                            problems.append(
                                f"{os.path.basename(wf)} runs {os.path.basename(module)}"
                                f"{' (live)' if is_live else ''} but never commits {state_file}"
                            )
        self.assertEqual(sorted(set(problems)), [], "\n" + "\n".join(sorted(set(problems))))

    def test_paper_trading_state_committed_where_paper_runs(self):
        """Regression for the 3.5-month freeze specifically."""
        for wf in ("alpaca-executor.yml", "position-book.yml"):
            path = os.path.join(WORKFLOW_DIR, wf)
            if not os.path.exists(path):
                continue
            self.assertIn("data/paper_trading_state.json", _committed_paths(open(path).read()),
                          f"{wf} runs the paper agent but drops its trading state")

    def test_recent_events_committed_by_market_monitor(self):
        path = os.path.join(WORKFLOW_DIR, "market_monitor.yml")
        self.assertIn("data/recent_events.json", _committed_paths(open(path).read()),
                      "market_monitor.py is the only writer of the events feed")


if __name__ == "__main__":
    unittest.main()
