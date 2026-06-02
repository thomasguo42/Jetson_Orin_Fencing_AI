#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parent
sys.path.append(str(BUNDLE_ROOT / "scripts"))
sys.path.append(str(BUNDLE_ROOT))

from reprocess_phrase_limb_interp_jumpsafe_experimental import main as extract_main  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full Raspberry Pi bundle pipeline on one phrase: extraction, repair, and FPS30 judging."
    )
    parser.add_argument("--phrase-dir", type=Path, required=True, help="Phrase folder containing the video and TXT.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for extracted/repaired artifacts. Defaults to bundle runtime_outputs.",
    )
    parser.add_argument("--yolo-conf", type=float, default=0.15)
    parser.add_argument("--yolo-verbose", action="store_true")
    parser.add_argument("--sam-threshold", type=float, default=0.15)
    parser.add_argument("--sam-mask-threshold", type=float, default=0.5)
    parser.add_argument("--repair-only", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def _load_fps30_decision(output_dir: Path) -> dict:
    output_json = output_dir / "analysis_result.json"
    if not output_json.exists():
        raise FileNotFoundError(f"Missing final FPS30 result: {output_json}")
    result = json.loads(output_json.read_text(encoding="utf-8"))
    decision = result.get("fps30_decision")
    return decision if isinstance(decision, dict) else result


def main() -> int:
    args = parse_args()
    argv = [
        "reprocess_phrase_limb_interp_jumpsafe_experimental.py",
        "--phrase-dir",
        str(args.phrase_dir.resolve()),
    ]
    if args.output_dir is not None:
        argv.extend(["--output-dir", str(args.output_dir.resolve())])
    argv.extend(["--yolo-conf", str(args.yolo_conf)])
    argv.extend(["--sam-threshold", str(args.sam_threshold)])
    argv.extend(["--sam-mask-threshold", str(args.sam_mask_threshold)])
    if args.yolo_verbose:
        argv.append("--yolo-verbose")
    if args.repair_only:
        argv.append("--repair-only")
    if args.progress:
        argv.append("--progress")

    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        extract_main()
    finally:
        sys.argv = old_argv

    output_dir = args.output_dir.resolve() if args.output_dir else (
        BUNDLE_ROOT / "runtime_outputs" / "experimental_limb_interp_jumpsafe" / args.phrase_dir.resolve().name
    )
    decision = _load_fps30_decision(output_dir)

    summary = {
        "output_dir": str(output_dir),
        "winner": decision.get("winner"),
        "reason": decision.get("reason"),
        "analysis_result_json": str(output_dir / "analysis_result.json"),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
