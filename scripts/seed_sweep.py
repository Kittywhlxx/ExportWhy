"""Accuracy across seeds: regenerate the world for each seed, triage every alert offline, compare with
the ground truth that the generator wrote for that seed. Produces a per-seed table and a per-scenario
confusion table (expected verdict x predicted verdict).

    .venv/bin/python scripts/seed_sweep.py [--seeds 1 2 3 ...] [--out docs/seed_sweep.md]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["SAMPLE_MODE"] = "true"

from agent import config  # noqa: E402
from agent import main as agent_main  # noqa: E402
from agent.roles import Task  # noqa: E402
from data import build_sample, gen_data  # noqa: E402

DEFAULT_SEEDS = [1, 2, 3, 5, 7, 11, 13, 19, 23, 42, 99, 2026]
VERDICTS = ["compliant", "investigate", "cannot-confirm"]


def sweep(seeds: list[int]) -> tuple[list[dict], dict]:
    per_seed = []
    confusion: dict[str, Counter] = defaultdict(Counter)  # scenario -> Counter(predicted)
    expected_of: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for seed in seeds:
            out = Path(tmp) / f"seed{seed}"
            gen_data.write(gen_data.gen(seed), out, seed)
            build_sample.SAMPLE, build_sample.DB = out, out / "dexa_sample.sqlite"
            build_sample.main()
            config.SAMPLE_DB = build_sample.DB
            truth = {r["alert_id"]: r for r in csv.DictReader(open(out / "ground_truth.csv"))}
            hits = 0
            misses = []
            for aid, t in truth.items():
                _, o = agent_main.run(Task(alert_id=aid), persist_memory=False)
                scen = t["scenario"].split(" ", 1)[1] if " " in t["scenario"] else t["scenario"]
                expected_of[scen] = t["expected_verdict"]
                confusion[scen][o["verdict"]] += 1
                if o["verdict"] == t["expected_verdict"]:
                    hits += 1
                else:
                    misses.append(f"{aid} {scen}: got {o['verdict']}")
            per_seed.append(
                {
                    "seed": seed,
                    "alerts": len(truth),
                    "correct": hits,
                    "misses": "; ".join(misses) or "-",
                }
            )
    return per_seed, {"confusion": confusion, "expected": expected_of}


def render(per_seed: list[dict], conf: dict) -> str:
    total = sum(r["alerts"] for r in per_seed)
    correct = sum(r["correct"] for r in per_seed)
    lines = [
        "# Accuracy across seeds",
        "",
        f"{len(per_seed)} seeds x 13 alerts = {total} triages, offline (SQLite twin, rule verdicts, stub model). "
        f"**{correct}/{total} verdicts match the generator's ground truth.**",
        "",
        "| seed | alerts | correct | misses |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r['seed']} | {r['alerts']} | {r['correct']} | {r['misses']} |" for r in per_seed]
    lines += [
        "",
        "## Confusion by injected scenario (rows: scenario, columns: predicted verdict)",
        "",
        "| scenario | expected | " + " | ".join(VERDICTS) + " |",
        "|---|---|" + "---|" * len(VERDICTS),
    ]
    for scen in sorted(conf["confusion"]):
        c = conf["confusion"][scen]
        lines.append(
            f"| {scen} | {conf['expected'][scen]} | "
            + " | ".join(str(c.get(v, 0)) for v in VERDICTS)
            + " |"
        )
    lines += [
        "",
        "Every row should have its whole count in the expected column. The generator guarantees that a background ticket never "
        "accidentally authorises an injected alert and that accounts with a named exception are reserved for the exception scenario; "
        "both guards were added after the sweep caught exactly those two leaks on seeds 11 and 3.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="*", default=DEFAULT_SEEDS)
    ap.add_argument("--out", default=str(ROOT / "docs/seed_sweep.md"))
    a = ap.parse_args()
    per_seed, conf = sweep(a.seeds)
    text = render(per_seed, conf)
    Path(a.out).write_text(text)
    print(text)
    return 0 if all(r["correct"] == r["alerts"] for r in per_seed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
