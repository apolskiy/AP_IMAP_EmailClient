# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.gate <run dir>...``: the quality gates over a run's results (design §9.7).

Exit 0 when every gate holds, 1 otherwise, 2 when there are no results.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from emi_harness.priorities import evaluate_gates
from emi_harness.results import load_results


def main(argv: Sequence[str] | None = None) -> int:
    """Print the gate report.

    Args:
        argv: Report directories (``reports/<run>`` or one job's directory); several for several OSes.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_harness.gate", description="EMI quality gates")
    parser.add_argument("directories", nargs="+", type=Path, help="run or job report directories")
    args = parser.parse_args(argv)
    outcomes = [outcome for directory in args.directories for outcome in load_results(directory)]
    if not outcomes:
        print("no results found", file=sys.stderr)
        return 2
    report = evaluate_gates(outcomes)
    print(report.render())
    return 0 if report.holds else 1


if __name__ == "__main__":
    sys.exit(main())
