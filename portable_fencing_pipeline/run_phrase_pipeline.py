#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BUNDLE_ROOT = Path(__file__).resolve().parent
sys.path.append(str(BUNDLE_ROOT / "scripts"))
sys.path.append(str(BUNDLE_ROOT))

import debug_referee_fps30 as fps30  # type: ignore
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


def _run_fps30(output_dir: Path) -> dict:
    txt_path = fps30.find_phrase_txt_file(output_dir)
    excel_path = fps30.find_phrase_excel_file(output_dir)
    video_path = fps30.find_phrase_video_file(output_dir)
    if txt_path is None or excel_path is None:
        raise FileNotFoundError(f"Missing TXT or keypoints Excel in {output_dir}")

    left_xdata, left_ydata, right_xdata, right_ydata = fps30.load_keypoints_from_excel(str(excel_path))
    phrase_fps = fps30.infer_phrase_fps(output_dir, txt_path=txt_path)
    phrase = fps30.parse_txt_file(
        str(txt_path),
        fps=phrase_fps,
        video_path=str(video_path) if video_path is not None else None,
    )
    if left_xdata and 16 in left_xdata:
        max_frame = len(left_xdata[16]) - 1
        fps30._trim_phrase_to_frames(phrase, max_frame)
    side_hit_events = fps30.extract_side_hit_events(
        str(txt_path),
        fps=phrase.fps,
        video_path=str(video_path) if video_path is not None else None,
    )

    experimental_json = output_dir / "analysis_result_limb_interp_experimental.json"
    norm_constant = None
    if experimental_json.exists():
        experimental = json.loads(experimental_json.read_text(encoding="utf-8"))
        norm_constant = experimental.get("normalisation_constant")

    decision = fps30.referee_decision(
        phrase,
        left_xdata,
        left_ydata,
        right_xdata,
        right_ydata,
        normalisation_constant=norm_constant,
        side_hit_events=side_hit_events,
    )
    raw_blade_relevant = fps30.has_relevant_blade_contact(phrase, side_hit_events) if hasattr(fps30, "has_relevant_blade_contact") else None
    judged_with_blade_rules = fps30.uses_blade_contact_rules(decision) if hasattr(fps30, "uses_blade_contact_rules") else None

    result_data = {}
    if experimental_json.exists():
        try:
            result_data = json.loads(experimental_json.read_text(encoding="utf-8"))
        except Exception:
            result_data = {}
    result_data["winner"] = decision.get("winner")
    result_data["reason"] = decision.get("reason")
    result_data["left_pauses"] = decision.get("left_pauses")
    result_data["right_pauses"] = decision.get("right_pauses")
    result_data["blade_analysis"] = decision.get("blade_analysis")
    result_data["blade_details"] = decision.get("blade_details")
    result_data["raw_blade_contact_relevant"] = raw_blade_relevant
    result_data["used_blade_contact_rules"] = judged_with_blade_rules
    result_data["speed_comparison"] = decision.get("speed_comparison")
    result_data["lunge_detected"] = decision.get("lunge_detected")
    result_data["left_arm_extensions"] = decision.get("left_arm_extensions", [])
    result_data["right_arm_extensions"] = decision.get("right_arm_extensions", [])
    result_data["fps30_decision"] = fps30.sanitize_for_json(decision)

    output_json = output_dir / "analysis_result.json"
    output_json.write_text(json.dumps(fps30.sanitize_for_json(result_data), indent=2), encoding="utf-8")
    return fps30.sanitize_for_json(decision)


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
    decision = _run_fps30(output_dir)

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
