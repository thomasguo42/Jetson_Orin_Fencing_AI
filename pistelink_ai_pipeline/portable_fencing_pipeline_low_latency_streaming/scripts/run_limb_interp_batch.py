#!/usr/bin/env python3
"""
Run the SAM-initialized limb-interp experimental pipeline over many phrase folders.

By default this scans `new_data/double_hit` and invokes
`reprocess_phrase_limb_interp_experimental.py` once per folder.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path("new_data/double_hit"),
        help="Directory containing phrase folders.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("results/experimental_limb_interp"),
        help="Per-phrase output root passed to the experimental script.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("results/experimental_limb_interp_batch"),
        help="Directory for batch summary and JSONL logs.",
    )
    parser.add_argument(
        "--start-after",
        type=str,
        default=None,
        help="Only process folders whose name is lexicographically greater than this marker.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of folders to process.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip folders that already have `analysis_result_limb_interp_experimental.json` in the output root.",
    )
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help="Run only the repair stage on existing keypoints in each phrase folder.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Forward `--progress` to the per-phrase experimental script.",
    )
    return parser.parse_args()


def find_phrase_dirs(base_dir: Path, start_after: str | None) -> list[Path]:
    folders = sorted([p for p in base_dir.iterdir() if p.is_dir()], key=lambda p: p.name)
    if start_after is None:
        return folders
    return [p for p in folders if p.name > start_after]


def result_json_path(output_root: Path, phrase_dir: Path) -> Path:
    return output_root / phrase_dir.name / "analysis_result_limb_interp_experimental.json"


def main() -> int:
    args = parse_args()
    base_dir = args.base_dir.resolve()
    output_root = args.output_root.resolve()
    log_dir = args.log_dir.resolve()
    script_path = (Path(__file__).resolve().parent / "reprocess_phrase_limb_interp_experimental.py").resolve()

    if not base_dir.exists():
        raise FileNotFoundError(f"Base dir not found: {base_dir}")
    if not script_path.exists():
        raise FileNotFoundError(f"Experimental script not found: {script_path}")

    output_root.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "run_all_double_hit.jsonl"
    summary_path = log_dir / "run_all_double_hit_summary.json"

    folders = find_phrase_dirs(base_dir, args.start_after)
    if args.skip_existing:
        folders = [p for p in folders if not result_json_path(output_root, p).exists()]
    if args.limit is not None:
        folders = folders[: args.limit]

    if not folders:
        print("No folders to process.")
        return 0

    counts = {"processed": 0, "skipped": 0, "errors": 0}
    started = time.time()

    with log_path.open("a", encoding="utf-8") as log:
        for index, folder in enumerate(folders, 1):
            rec: dict[str, object] = {
                "index": index,
                "total": len(folders),
                "folder": str(folder),
                "name": folder.name,
                "status": None,
            }

            phrase_output_dir = output_root / folder.name
            if args.skip_existing and result_json_path(output_root, folder).exists():
                rec["status"] = "skipped_existing"
                counts["skipped"] += 1
                log.write(json.dumps(rec) + "\n")
                log.flush()
                continue

            t0 = time.time()
            print(f"[{index}/{len(folders)}] {folder.name} ...", flush=True)

            cmd = [
                sys.executable,
                str(script_path),
                "--phrase-dir",
                str(folder),
                "--output-dir",
                str(phrase_output_dir),
            ]
            if args.repair_only:
                cmd.append("--repair-only")
            if args.progress:
                cmd.append("--progress")

            proc = subprocess.run(cmd, capture_output=True, text=True)
            rec["returncode"] = proc.returncode
            rec["seconds"] = round(time.time() - t0, 3)
            if proc.stdout:
                rec["stdout"] = proc.stdout[-20000:]
            if proc.stderr:
                rec["stderr"] = proc.stderr[-20000:]

            if proc.returncode == 0:
                counts["processed"] += 1
                rec["status"] = "ok"
                try:
                    rec["summary"] = json.loads(proc.stdout.strip())
                except Exception:
                    rec["summary_parse_error"] = True
            else:
                counts["errors"] += 1
                rec["status"] = "error"

            log.write(json.dumps(rec) + "\n")
            log.flush()

            summary = {
                "counts": counts,
                "started_at_epoch": started,
                "elapsed_seconds": round(time.time() - started, 3),
                "last_completed": folder.name,
                "log_path": str(log_path),
            }
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {"status": rec["status"], "name": folder.name, "seconds": rec["seconds"]},
                    indent=2,
                ),
                flush=True,
            )

    final_summary = {
        "counts": counts,
        "started_at_epoch": started,
        "elapsed_seconds": round(time.time() - started, 3),
        "completed_all": True,
        "log_path": str(log_path),
    }
    summary_path.write_text(json.dumps(final_summary, indent=2), encoding="utf-8")
    print(json.dumps(final_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
