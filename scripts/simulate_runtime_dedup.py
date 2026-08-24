#!/usr/bin/env python3
"""handover §7 (8/23 design): replay the runtime dedup rule over recorded rows.

`scan_duplicate_resend.py` classifies a cluster only after seeing all of it: it
needs two distinct sentences and contiguous originals, both of which are facts
about the whole cluster. The runtime has to decide when a single sentence
arrives, so the same judgement is restated causally — a machine replay walks
previously published sentences IN ORDER, a speaker does not re-utter several
different sentences in their original order:

  short sentence                  -> publish            (indistinguishable from speech)
  no match in history             -> publish, record, run reset
  match at history position j     -> publish, cursor = j (first hit could be a person)
  match at cursor + 1             -> DROP, cursor advances
  match, cursor not continuous    -> publish, run restarts at j

A dropped sentence is never recorded: history holds what was PUBLISHED, so a
replay always matches the original rather than an earlier copy of itself.

This script feeds the recorded CSV through that rule one sentence at a time and
checks the outcome against the scanner's own classification of the same file:

  1. no unclassified single-sentence duplicate is dropped   (hard condition)
  2. every known block re-send loses at least one sentence
  3. nothing outside a known block is dropped

Usage:
  scripts/simulate_runtime_dedup.py --csv ~/neemba-logs/translations-2026-08-23-30d.csv
  scripts/simulate_runtime_dedup.py --csv tr.csv --sweep
  scripts/simulate_runtime_dedup.py --csv tr.csv --compression-guard 0.6
"""
import argparse
import sys
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The scanner owns the detector; importing it keeps the ground truth this
# simulator is scored against from drifting away from the one in the report.
from scan_duplicate_resend import (  # noqa: E402
    find_blocks,
    find_pairs,
    kst,
    load,
    normalize,
)

# --- runtime rule parameters (confirmed against the 30-day export) ---
# The shortest sentence a real re-send carried was 12 normalized chars and the
# weakest true match scored 0.94, so both thresholds sit one notch inside those
# observations. Loosening either only widens the false-positive surface: at
# MIN_CHARS=8/SIM=0.85 thirteen rows sit one consecutive match away from a drop,
# at 10/0.90 six do, and the sentences actually dropped are the same either way.
MIN_CHARS = 10
SIM_THRESHOLD = 0.90
LOOKBACK_SEC = 300.0
HISTORY_MAX = 200


def _similar(a: str, b: str, threshold: float) -> bool:
    """difflib ratio with a length prune.

    ratio() is bounded above by 2*min(len)/(len(a)+len(b)), so texts of very
    different length can be rejected without running the matcher at all — the
    sweep compares every arrival against a 200-sentence history and would
    otherwise spend minutes inside SequenceMatcher.
    """
    la, lb = len(a), len(b)
    if 2 * min(la, lb) < threshold * (la + lb):
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def simulate_session(rows: list, *, min_chars: int, sim_threshold: float,
                     lookback_sec: float, compression_guard: float | None) -> list:
    """Run the rule over one session's rows in arrival order; return drops."""
    history: deque = deque(maxlen=HISTORY_MAX)  # (abs_idx, norm, ts) as published
    next_idx = 0
    cursor: int | None = None
    # First arrival of the current run, used only by the compression guard.
    run_start_dup_ts = run_start_src_ts = None
    dropped, matched_passes = [], []

    for row in rows:
        norm = row["norm"]
        if len(norm) < min_chars:
            continue

        best = None
        for abs_idx, prev_norm, prev_ts in reversed(history):
            if (row["ts"] - prev_ts).total_seconds() > lookback_sec:
                break
            if not _similar(prev_norm, norm, sim_threshold):
                continue
            if best is None:
                best = (abs_idx, prev_ts)  # newest match: the replay's original
            if cursor is not None and abs_idx == cursor + 1:
                best = (abs_idx, prev_ts)  # continuing the run wins
                break

        if best is None:
            history.append((next_idx, norm, row["ts"]))
            next_idx += 1
            cursor = None
            continue

        abs_idx, src_ts = best
        if cursor is not None and abs_idx == cursor + 1:
            if compression_guard is not None:
                # A replay is squeezed into far less time than the original
                # span; a sung refrain keeps its tempo. Only the ratio can tell
                # a two-line refrain from a two-sentence replay.
                src_span = (src_ts - run_start_src_ts).total_seconds()
                dup_span = (row["ts"] - run_start_dup_ts).total_seconds()
                if dup_span > compression_guard * src_span:
                    cursor = abs_idx
                    run_start_dup_ts, run_start_src_ts = row["ts"], src_ts
                    continue
            cursor = abs_idx
            dropped.append(row)
            continue

        # A lone match is a person repeating himself until a second one lines up.
        # These are the only rows a drop can ever grow out of, so counting them
        # is how close the rule came to a false positive on this corpus.
        matched_passes.append(row)
        cursor = abs_idx
        run_start_dup_ts, run_start_src_ts = row["ts"], src_ts

    return dropped, matched_passes


def ground_truth(sessions: dict) -> dict:
    """The scanner's verdict on the same file, indexed by translation id."""
    truth = {"blocks": [], "block_ids": set(), "rejected_ids": set(),
             "single_ids": set(), "label": {}}
    for sid, rows in sessions.items():
        pairs = find_pairs(rows)
        blocks, rejected = find_blocks(pairs, rows)
        clustered = {id(p) for b in blocks for p in b}
        clustered |= {id(p) for c, _ in rejected for p in c}

        for block in blocks:
            ids = {p["dup"]["id"] for p in block}
            truth["blocks"].append({"sid": sid, "ts": block[0]["dup"]["ts"], "ids": ids})
            truth["block_ids"] |= ids
            for i in ids:
                truth["label"][i] = "block"
        for cluster, why in rejected:
            for p in cluster:
                truth["rejected_ids"].add(p["dup"]["id"])
                truth["label"][p["dup"]["id"]] = f"rejected ({why})"
        for p in pairs:
            if id(p) not in clustered:
                truth["single_ids"].add(p["dup"]["id"])
                truth["label"][p["dup"]["id"]] = "unclassified single"
    return truth


def evaluate(sessions: dict, truth: dict, **params) -> dict:
    dropped, matched = [], []
    for rows in sessions.values():
        d, m = simulate_session(rows, **params)
        dropped.extend(d)
        matched.extend(m)
    dropped_ids = {r["id"] for r in dropped}

    per_block = [len(b["ids"] & dropped_ids) for b in truth["blocks"]]
    return {
        "dropped": dropped,
        "matched": matched,
        "matched_outside": [r for r in matched if r["id"] not in truth["block_ids"]],
        "dropped_ids": dropped_ids,
        "c1_singles": dropped_ids & truth["single_ids"],
        "c2_missed": [b for b, n in zip(truth["blocks"], per_block) if n == 0],
        "c3_outside": dropped_ids - truth["block_ids"],
        "per_block": per_block,
        # `all([])` is True: without the count check an export holding no known
        # block re-send would pass while verifying nothing. The 30-day window is
        # not reproducible after 2026-09-22, so a later run WILL use another file.
        "passed": (not (dropped_ids & truth["single_ids"])
                   and truth["blocks"] and all(per_block)
                   and not (dropped_ids - truth["block_ids"])),
    }


def report(sessions: dict, truth: dict, res: dict, params: dict) -> None:
    total_rows = sum(len(r) for r in sessions.values())
    print(f"parameters: MIN_CHARS={params['min_chars']} "
          f"SIM_THRESHOLD={params['sim_threshold']} "
          f"LOOKBACK_SEC={params['lookback_sec']:.0f} "
          f"compression_guard={params['compression_guard']}")
    print(f"corpus: {total_rows} rows, {len(sessions)} sessions, "
          f"{len(truth['blocks'])} known blocks ({len(truth['block_ids'])} sentences), "
          f"{len(truth['single_ids'])} unclassified singles\n")

    print(f"dropped {len(res['dropped'])} rows")
    for row in sorted(res["dropped"], key=lambda r: r["ts"]):
        label = truth["label"].get(row["id"], "OUTSIDE any cluster")
        print(f"  {kst(row['ts'])} id{row['id']:<7}[{label}]  {row['text'][:44]}")

    outside = res["matched_outside"]
    print(f"\nmatched-but-published rows outside known blocks: {len(outside)} "
          f"(each is one consecutive match away from a false drop)")
    for row in sorted(outside, key=lambda r: r["ts"]):
        label = truth["label"].get(row["id"], "no cluster")
        print(f"  {kst(row['ts'])} id{row['id']:<7}[{label}]  {row['text'][:44]}")

    print("\nper known block (dropped / total):")
    for block, n in zip(truth["blocks"], res["per_block"]):
        mark = "ok " if n else "MISS"
        print(f"  {mark} {block['sid'][:8]} @{kst(block['ts'])}  {n}/{len(block['ids'])}")

    print("\nconditions:")
    print(f"  1. unclassified singles dropped: {len(res['c1_singles'])} "
          f"({'PASS' if not res['c1_singles'] else 'FAIL'})")
    ok2 = truth["blocks"] and not res["c2_missed"]
    print(f"  2. blocks with no drop: {len(res['c2_missed'])} of "
          f"{len(truth['blocks'])} known ({'PASS' if ok2 else 'FAIL'})")
    print(f"  3. drops outside known blocks: {len(res['c3_outside'])} "
          f"({'PASS' if not res['c3_outside'] else 'FAIL'})")
    print(f"\n{'PASS' if res['passed'] else 'FAIL'}")


SWEEP_MIN_CHARS = (6, 8, 10, 12)
SWEEP_SIM = (0.80, 0.85, 0.90, 0.95)
SWEEP_LOOKBACK = (120.0, 300.0, 600.0)
SWEEP_GUARD = (None, 0.6)


def sweep(sessions: dict, truth: dict) -> None:
    print(f"{'chars':>6}{'sim':>6}{'look':>6}{'guard':>7}{'drop':>6}"
          f"{'c1':>4}{'c2':>4}{'c3':>4}  blocks         verdict")
    for guard in SWEEP_GUARD:
        for min_chars in SWEEP_MIN_CHARS:
            for sim_threshold in SWEEP_SIM:
                for lookback_sec in SWEEP_LOOKBACK:
                    res = evaluate(sessions, truth, min_chars=min_chars,
                                   sim_threshold=sim_threshold,
                                   lookback_sec=lookback_sec,
                                   compression_guard=guard)
                    per = "/".join(str(n) for n in res["per_block"])
                    print(f"{min_chars:>6}{sim_threshold:>6.2f}{lookback_sec:>6.0f}"
                          f"{str(guard):>7}{len(res['dropped']):>6}"
                          f"{len(res['c1_singles']):>4}{len(res['c2_missed']):>4}"
                          f"{len(res['c3_outside']):>4}  {per:<15}"
                          f"{'PASS' if res['passed'] else 'fail'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True, help="export written by scan_duplicate_resend --dump-csv")
    ap.add_argument("--sweep", action="store_true", help="grid over the rule parameters")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS)
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD)
    ap.add_argument("--lookback-sec", type=float, default=LOOKBACK_SEC)
    ap.add_argument("--compression-guard", type=float, default=None,
                    help="drop only if the replay span is at most this fraction "
                         "of the original span (off by default)")
    args = ap.parse_args()

    sessions = load(open(args.csv, encoding="utf-8").read())
    if not sessions:
        raise SystemExit("no sessions in export")
    truth = ground_truth(sessions)

    if args.sweep:
        sweep(sessions, truth)
        return 0

    params = {"min_chars": args.min_chars, "sim_threshold": args.sim_threshold,
              "lookback_sec": args.lookback_sec,
              "compression_guard": args.compression_guard}
    res = evaluate(sessions, truth, **params)
    report(sessions, truth, res, params)
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
