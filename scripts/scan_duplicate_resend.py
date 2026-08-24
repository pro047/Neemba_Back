#!/usr/bin/env python3
"""observation-2026-08-23 §4-1-1: scan past sessions for duplicate subtitle re-sends.

A stalled STT stream makes Google replay an earlier span. node's computeDelta sees
an already-reset prevText and returns the whole text as a delta, so a block of
sentences is published again under fresh sequence numbers — and every dedup layer
in the pipeline is sequence-based, so none of them stop it.

Exact matching is not enough: the replay is re-recognized audio, so the text drifts
by a character or two (`십자가의` -> `십자가에`). This scans with fuzzy similarity
instead, then separates machine re-sends from a speaker genuinely repeating himself.

A cluster of duplicates counts as a BLOCK RE-SEND only when all three hold:
  - it carries at least two DISTINCT sentences (a refrain repeated by a person is
    one sentence over and over; a replayed span is several different ones)
  - those sentences land within seconds of each other
  - their originals are contiguous in the session, counting only rows long enough
    to be matchable at all (short interjections sit between originals and must not
    break the run)

Usage:
  scripts/scan_duplicate_resend.py                 # pull from prod over ssh
  scripts/scan_duplicate_resend.py --csv tr.csv    # re-scan a saved export
  scripts/scan_duplicate_resend.py --dump-csv tr.csv

Requires SSH reachability to prod (see .claude/skills/watch-service). Read-only:
it runs a single SELECT and never writes to the database.
"""
import argparse
import csv
import datetime as dt
import io
import re
import subprocess
import sys
from collections import defaultdict
from difflib import SequenceMatcher

SSH_HOST = "ubuntu@13.125.26.93"
SSH_KEY = "~/Downloads/neemba.pem"
PSQL = "docker exec -i postgres psql -U neemba -d neemba_monitor"

# Sessions below this row count are tests and one-off connects, not services.
MIN_SESSION_ROWS = 100

# psql meta-commands must fit on one line, so \copy is assembled rather than
# written as a block literal.
EXPORT_QUERY = (
    "SELECT session_id, id, sequence, created_at, source_text FROM app.translations"
    " WHERE session_id IN (SELECT session_id FROM app.translations"
    f" GROUP BY session_id HAVING count(*) >= {MIN_SESSION_ROWS})"
    " ORDER BY session_id, created_at, id"
)
EXPORT_SQL = f"\\copy ({EXPORT_QUERY}) TO STDOUT WITH (FORMAT csv, HEADER true)\n"

# --- detector parameters (documented in observation-2026-08-23.md §4-1-1) ---
LOOKBACK_SEC = 300.0   # how far back a replay may land
MIN_CHARS = 8          # below this it is `아멘`/`감사합니다`, indistinguishable from real speech
SIM_THRESHOLD = 0.85   # difflib ratio on normalized text
CLUSTER_SEC = 15.0     # replays this close together belong to one cluster
MIN_DISTINCT = 2       # distinct sentences a cluster needs to be a machine re-send
SLACK_ROWS = 1         # contiguity slack: a replay may merge or split one boundary
KST = dt.timezone(dt.timedelta(hours=9))

NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def normalize(text: str) -> str:
    return NON_WORD.sub("", text)


def kst(ts: dt.datetime) -> str:
    return ts.astimezone(KST).strftime("%m-%d %H:%M:%S")


def fetch_csv() -> str:
    cmd = ["ssh", "-i", SSH_KEY, SSH_HOST, PSQL]
    proc = subprocess.run(cmd, input=EXPORT_SQL, capture_output=True,
                          text=True, encoding="utf-8")
    # psql can stream rows and then fail, exiting 0 with a partial export, so a
    # silent undercount is only ruled out by treating any stderr as failure.
    if proc.returncode != 0 or proc.stderr.strip() or not proc.stdout.strip():
        raise SystemExit(f"export failed:\n{proc.stderr.strip() or '(empty result)'}")
    return proc.stdout


def load(text: str) -> dict:
    sessions = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text)):
        sessions[row["session_id"]].append({
            "id": int(row["id"]),
            "seq": int(row["sequence"] or -1),
            "ts": dt.datetime.fromisoformat(row["created_at"]),
            "text": row["source_text"],
            "norm": normalize(row["source_text"]),
        })
    for rows in sessions.values():
        rows.sort(key=lambda r: (r["ts"], r["id"]))
    return sessions


def find_pairs(rows: list) -> list:
    """For each row, the most similar earlier ORIGINAL inside the lookback window.

    Rows already identified as duplicates are not offered as sources: in a replayed
    span every copy would otherwise chain to the copy before it, understating both
    the delay and the width of the original span.
    """
    pairs, duplicates = [], set()
    for i, cur in enumerate(rows):
        if len(cur["norm"]) < MIN_CHARS:
            continue
        best = None
        for j in range(i - 1, -1, -1):
            prev = rows[j]
            if (cur["ts"] - prev["ts"]).total_seconds() > LOOKBACK_SEC:
                break
            if len(prev["norm"]) < MIN_CHARS or j in duplicates:
                continue
            ratio = SequenceMatcher(None, prev["norm"], cur["norm"]).ratio()
            if ratio >= SIM_THRESHOLD and (best is None or ratio > best["sim"]):
                best = {"sim": ratio, "src_idx": j, "src": prev}
        if best:
            duplicates.add(i)
            pairs.append({
                "src": best["src"], "dup": cur, "src_idx": best["src_idx"],
                "sim": best["sim"],
                "delay": (cur["ts"] - best["src"]["ts"]).total_seconds(),
            })
    return pairs


def find_blocks(pairs: list, rows: list) -> tuple:
    """Split time clusters into machine re-sends and rejected ones.

    Contiguity is measured over matchable rows only. Short interjections are
    invisible to find_pairs, so counting raw indices would let a single `아멘`
    between two originals break an otherwise solid run.
    """
    rank = {}
    for i, row in enumerate(rows):
        if len(row["norm"]) >= MIN_CHARS:
            rank[i] = len(rank)

    clusters, current = [], []
    for pair in sorted(pairs, key=lambda p: p["dup"]["ts"]):
        if current and (pair["dup"]["ts"] - current[-1]["dup"]["ts"]).total_seconds() <= CLUSTER_SEC:
            current.append(pair)
        else:
            if current:
                clusters.append(current)
            current = [pair]
    if current:
        clusters.append(current)

    blocks, rejected = [], []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        distinct = len({p["dup"]["norm"] for p in cluster})
        positions = [rank[p["src_idx"]] for p in cluster]
        spanned = max(positions) - min(positions) + 1
        if distinct < MIN_DISTINCT:
            rejected.append((cluster, f"one sentence repeated (distinct={distinct})"))
        elif spanned > len(cluster) + SLACK_ROWS:
            rejected.append((cluster, f"originals not contiguous (spanned={spanned})"))
        else:
            blocks.append(cluster)
    return blocks, rejected


def report(sessions: dict) -> int:
    results = []
    for sid, rows in sessions.items():
        pairs = find_pairs(rows)
        blocks, rejected = find_blocks(pairs, rows)
        results.append((sid, rows, pairs, blocks, rejected))
    results.sort(key=lambda r: r[1][0]["ts"])

    print(f"{'start KST':<15}{'session':<10}{'rows':>6}{'min':>6}{'pairs':>7}{'blocks':>8}  replayed sentences")
    total_min = total_rows = total_blocks = total_sent = 0
    for sid, rows, pairs, blocks, _ in results:
        minutes = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 60
        total_min += minutes
        total_rows += len(rows)
        total_blocks += len(blocks)
        total_sent += sum(len(b) for b in blocks)
        detail = " · ".join(f"{kst(b[0]['dup']['ts'])[6:]} x{len(b)}" for b in blocks) or "-"
        print(f"{kst(rows[0]['ts']):<15}{sid[:8]:<10}{len(rows):>6}{minutes:>6.0f}"
              f"{len(pairs):>7}{len(blocks):>8}  {detail}")

    affected = sum(1 for r in results if r[3])
    print(f"\n{total_blocks} block re-sends / {total_sent} sentences over "
          f"{total_min:.0f} min, {total_rows} rows, {len(results)} sessions "
          f"({affected} affected)")

    for sid, rows, pairs, blocks, _ in results:
        for block in blocks:
            src_span = (max(p["src"]["ts"] for p in block)
                        - min(p["src"]["ts"] for p in block)).total_seconds()
            print(f"\n--- {sid[:8]} @{kst(block[0]['dup']['ts'])}  "
                  f"{len(block)} sentences | source window {src_span:.0f}s | "
                  f"delay {block[0]['delay']:.0f}s")
            for p in block:
                print(f"    {kst(p['src']['ts'])[6:]} seq{p['src']['seq']:<6}"
                      f"-> {kst(p['dup']['ts'])[6:]} seq{p['dup']['seq']:<6}"
                      f"sim{p['sim']:.2f}  {p['dup']['text'][:44]}")

    # Rejected clusters are where the criteria earn or lose their keep; hiding
    # them among the isolated pairs would make the thresholds unauditable.
    print("\nclusters rejected by the block criteria:")
    any_rejected = False
    for sid, rows, pairs, blocks, rejected in results:
        for cluster, why in rejected:
            any_rejected = True
            print(f"  {sid[:8]} @{kst(cluster[0]['dup']['ts'])} x{len(cluster)} — {why}")
            for p in cluster:
                print(f"      {kst(p['dup']['ts'])[6:]} delay{p['delay']:6.0f}s  {p['dup']['text'][:40]}")
    if not any_rejected:
        print("  (none)")

    # Single-sentence duplicates cannot be told apart from a speaker repeating himself.
    print("\nunclassified single-sentence duplicates (delays):")
    for sid, rows, pairs, blocks, rejected in results:
        clustered = {id(p) for b in blocks for p in b}
        clustered |= {id(p) for c, _ in rejected for p in c}
        isolated = [p for p in pairs if id(p) not in clustered]
        if isolated:
            delays = ", ".join(f"{p['delay']:.0f}s" for p in isolated)
            print(f"  {kst(rows[0]['ts'])[:5]} {sid[:8]}: {len(isolated)}  ({delays})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="scan a saved export instead of querying prod")
    ap.add_argument("--dump-csv", help="write the fetched export here before scanning")
    args = ap.parse_args()

    raw = open(args.csv, encoding="utf-8").read() if args.csv else fetch_csv()
    if args.dump_csv:
        open(args.dump_csv, "w", encoding="utf-8").write(raw)

    sessions = load(raw)
    if not sessions:
        raise SystemExit("no sessions with >= %d rows" % MIN_SESSION_ROWS)
    return report(sessions)


if __name__ == "__main__":
    sys.exit(main())
