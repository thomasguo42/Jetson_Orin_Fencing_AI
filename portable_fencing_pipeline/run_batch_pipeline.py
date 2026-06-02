#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full Pi bundle pipeline on many phrase folders.")
    parser.add_argument("--base-dir", type=Path, default=BUNDLE_ROOT / "runtime_inputs")
    parser.add_argument("--output-root", type=Path, default=BUNDLE_ROOT / "runtime_outputs" / "experimental_limb_interp_jumpsafe")
    parser.add_argument("--log-dir", type=Path, default=BUNDLE_ROOT / "logs" / "full_pipeline_batch")
    parser.add_argument("--start-after", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--repair-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = args.base_dir.resolve()
    output_root = args.output_root.resolve()
    log_dir = args.log_dir.resolve()
    runner = (BUNDLE_ROOT / "run_phrase_pipeline.py").resolve()

    output_root.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run_batch.jsonl"
    summary_path = log_dir / "run_batch_summary.json"

    folders = sorted([p for p in base_dir.iterdir() if p.is_dir()], key=lambda p: p.name)
    if args.start_after is not None:
        folders = [p for p in folders if p.name > args.start_after]
    if args.limit is not None:
        folders = folders[: args.limit]

    counts = {"processed": 0, "skipped": 0, "errors": 0}
    started = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        for index, folder in enumerate(folders, 1):
            output_dir = output_root / folder.name
            if args.skip_existing and (output_dir / "analysis_result.json").exists():
                counts["skipped"] += 1
                log.write(json.dumps({"index": index, "name": folder.name, "status": "skipped_existing"}) + "\n")
                log.flush()
                continue

            cmd = [sys.executable, str(runner), "--phrase-dir", str(folder), "--output-dir", str(output_dir)]
            if args.progress:
                cmd.append("--progress")
            if args.repair_only:
                cmd.append("--repair-only")
            t0 = time.time()
            proc = subprocess.run(cmd, capture_output=True, text=True)
            rec = {
                "index": index,
                "total": len(folders),
                "name": folder.name,
                "returncode": proc.returncode,
                "seconds": round(time.time() - t0, 3),
                "stdout": proc.stdout[-20000:] if proc.stdout else "",
                "stderr": proc.stderr[-20000:] if proc.stderr else "",
                "status": "ok" if proc.returncode == 0 else "error",
            }
            if proc.returncode == 0:
                counts["processed"] += 1
            else:
                counts["errors"] += 1
            log.write(json.dumps(rec) + "\n")
            log.flush()
            summary_path.write_text(
                json.dumps(
                    {
                        "counts": counts,
                        "elapsed_seconds": round(time.time() - started, 3),
                        "last_completed": folder.name,
                        "log_path": str(log_path),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    summary_path.write_text(
        json.dumps(
            {
                "counts": counts,
                "elapsed_seconds": round(time.time() - started, 3),
                "log_path": str(log_path),
                "completed_all": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(summary_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
