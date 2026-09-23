"""Print what the experiment registry knows: the honest denominator.

    python scripts/registry.py            # summary per data source
    python scripts/registry.py --trials   # every trial, oldest first

Read-only. Trials are added by the harness that runs them, never by hand
through this script, so that registering always happens before a result
exists.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.research.registry import SEALED_SOURCE, SEALED_START, Registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", default=str(ROOT / "research" / "registry.jsonl"))
    parser.add_argument("--trials", action="store_true")
    args = parser.parse_args()

    reg = Registry.load(args.path)
    sources = sorted({u.source for t in reg.trials for u in t.uses})

    if args.trials:
        for t in reg.trials:
            print(f"{t.registered:%Y-%m-%d %H:%M}  {reg.status_of(t.id):9}  "
                  f"tests {t.tests:>2}  configs {t.configurations:>3}  {t.id}")
        print()

    for source in sources:
        print(f"{source}")
        print(f"  verdicts read      : {reg.tests_on(source)}")
        print(f"  configurations run : {reg.configurations_on(source)}")
        print(f"  next test must beat: |t| > {reg.threshold_for_next(source):.2f}"
              f"  (Bonferroni, two-sided 5%)")
        exposure = reg.exposure_by_year(source)
        print("  exposure per year  : " + ", ".join(f"{y}:{n}" for y, n in exposure.items()))

    sealed = reg.is_unseen(SEALED_SOURCE, SEALED_START, date.max)
    print(f"\nsealed holdout ({SEALED_SOURCE} from {SEALED_START}): "
          f"{'SEALED — never used' if sealed else 'SPENT'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
