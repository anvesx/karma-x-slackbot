"""SQLite-only local benchmark, not Slack throughput or a GCE performance claim."""

import argparse
import json
import math
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from karma_x.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--awards", type=int, default=2000)
    parser.add_argument("--writers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.awards <= 100000 or not 1 <= args.writers <= 32:
        parser.error("Use 1–100000 awards and 1–32 writers.")
    with tempfile.TemporaryDirectory(prefix="karma-x-benchmark-") as directory:
        store = Store(Path(directory) / "benchmark.db")

        def award(i: int) -> tuple[bool, float]:
            start = time.perf_counter()
            try:
                store.give(str(i), "TTEST", "UGIVER", "U" + str(i % 100), "CTEST", "synthetic", i)
                return True, (time.perf_counter() - start) * 1000
            except sqlite3.OperationalError:
                return False, (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.writers) as pool:
            results = list(pool.map(award, range(args.awards)))
        duration = time.perf_counter() - start
        successful = sum(ok for ok, _ in results)
        latencies = sorted(ms for _, ms in results)
        with store.connection() as db:
            rows = db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0]
            count = db.execute("SELECT SUM(active_karma) FROM users").fetchone()[0]
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if rows != successful or count != successful or integrity != "ok":
            raise RuntimeError("Ledger/count/integrity mismatch.")
        begin = time.perf_counter()
        store.top("TTEST")
        top_ms = (time.perf_counter() - begin) * 1000
        begin = time.perf_counter()
        store.winners("TTEST", 0, args.awards)
        winners_ms = (time.perf_counter() - begin) * 1000
        print(
            json.dumps(
                {
                    "scope": "local SQLite only, temporary synthetic data",
                    "attempted": args.awards,
                    "writers": args.writers,
                    "committed": successful,
                    "busy_errors": args.awards - successful,
                    "elapsed_s": round(duration, 3),
                    "committed_per_s": round(successful / duration, 1),
                    "p95_ms_all_attempts": round(
                        latencies[math.ceil(len(latencies) * 0.95) - 1], 2
                    ),
                    "top30_ms": round(top_ms, 2),
                    "winners_ms": round(winners_ms, 2),
                    "integrity": integrity,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
