#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

# Import the debug referee logic
sys.path.append(str(Path(__file__).parent))
import debug_referee_fps30 as debug_referee_module
from debug_referee_fps30 import (
    extract_side_hit_events,
    find_phrase_excel_file,
    find_phrase_txt_file,
    find_phrase_video_file,
    infer_phrase_fps,
    load_keypoints_from_excel,
    parse_txt_file,
    referee_decision,
    sanitize_for_json,
)

WINNER_PRIORITIES = [
    "Confirmed result winner",
    "Remote referee winner overrides manual selection",
    "Remote referee winner",
    "Manual selection winner",
]

def _earliest_hit_time(phrase, side_hit_events: Optional[dict]) -> Optional[float]:
    """Return earliest hit time from explicit side-hit events, fallback to simultaneous hit."""
    candidates = []
    if side_hit_events:
        for key in ("left_scores_on_right", "right_scores_on_left"):
            for event in side_hit_events.get(key, []):
                t = event.get("time")
                if isinstance(t, (int, float)):
                    candidates.append(float(t))
    if phrase.simultaneous_hit_time is not None:
        candidates.append(float(phrase.simultaneous_hit_time))
    return min(candidates) if candidates else None


def has_relevant_blade_contact(phrase, side_hit_events: Optional[dict]) -> bool:
    """True when a blade contact is within 1s before the earliest hit and before lockout."""
    hit_time = _earliest_hit_time(phrase, side_hit_events)
    if hit_time is None:
        return False
    for bc in phrase.blade_contacts:
        if (hit_time - 1.0) <= bc.time < hit_time:
            if phrase.lockout_start is None or bc.time < phrase.lockout_start:
                return True
    return False


def uses_blade_contact_rules(decision: dict) -> bool:
    """True only when the integrated referee decision treated contact as non-accident."""
    blade_details = decision.get("blade_details")
    if not isinstance(blade_details, dict):
        return False

    accident_prediction = blade_details.get("accident_prediction")
    if not isinstance(accident_prediction, dict):
        return False

    return not bool(accident_prediction.get("predicted_is_accident"))

def _extract_winner(content: str, label: str) -> Optional[str]:
    pattern = rf"{re.escape(label)}:\s*(?P<winner>Right|Left|Abstain)(?:\\s+Fencer)?(?:\\s*\\([^)]*\\))?"
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None
    winner = match.group("winner").strip().lower()
    return winner if winner in {"left", "right", "abstain"} else None


def get_actual_winner(txt_path: str) -> Optional[str]:
    """Extract the actual winner from txt file across historical formats."""
    try:
        with open(txt_path, 'r') as f:
            content = f.read()

        for label in WINNER_PRIORITIES:
            winner = _extract_winner(content, label)
            if winner:
                return winner

        return None
    except Exception as e:
        print(f"Error reading {txt_path}: {e}")
        return None


def prediction_matches(actual: str, predicted: Optional[str]) -> bool:
    if predicted is None:
        return False
    if actual == 'abstain':
        return predicted in {'left', 'right', 'abstain'}
    return actual == predicted

def main():
    parser = argparse.ArgumentParser(description="Re-run debug_referee_fps30 across existing training data and collect mismatches.")
    bundle_root = Path(__file__).resolve().parent.parent
    parser.add_argument('--root', type=Path, default=bundle_root / 'runtime_outputs' / 'experimental_limb_interp_jumpsafe')
    parser.add_argument('--correct-dir', type=Path, default=bundle_root / 'runtime_outputs' / 'fps30_validation' / 'correct_results')
    parser.add_argument('--mismatch-dir', type=Path, default=bundle_root / 'runtime_outputs' / 'fps30_validation' / 'mismatched_results')
    parser.add_argument('--limit', type=int, default=None)
    args = parser.parse_args()

    training_dir = args.root
    mismatch_dir = args.mismatch_dir
    correct_dir = args.correct_dir
    
    # Clean up directories
    if mismatch_dir.exists():
        shutil.rmtree(mismatch_dir)
    mismatch_dir.mkdir(parents=True, exist_ok=True)
    
    if correct_dir.exists():
        shutil.rmtree(correct_dir)
    correct_dir.mkdir(parents=True, exist_ok=True)

    mismatch_blade_dir = mismatch_dir / "blade_contact"
    mismatch_other_dir = mismatch_dir / "no_blade_contact"
    correct_blade_dir = correct_dir / "blade_contact"
    correct_other_dir = correct_dir / "no_blade_contact"
    for d in [mismatch_blade_dir, mismatch_other_dir, correct_blade_dir, correct_other_dir]:
        d.mkdir(parents=True, exist_ok=True)
    
    total_processed = 0
    total_checked = 0
    mismatches_found = 0
    errors = 0
    skipped_no_winner = 0
    skipped_no_excel = 0
    bucket_counts = {
        "correct_blade": 0,
        "correct_no_blade": 0,
        "mismatch_blade": 0,
        "mismatch_no_blade": 0,
    }
    
    print(f"Scanning {training_dir}...")
    print("=" * 80)
    
    subdirs = sorted([d for d in training_dir.iterdir() if d.is_dir()])
    
    processed = 0
    for item in subdirs:
        if args.limit is not None and processed >= args.limit:
            break
        # Find necessary files
        txt_path = find_phrase_txt_file(item)
        excel_path = find_phrase_excel_file(item)
        video_path = find_phrase_video_file(item)
        json_path = item / "analysis_result.json"
        
        if txt_path is None:
            continue
        
        if excel_path is None:
            skipped_no_excel += 1
            continue
        
        try:
            total_processed += 1
            
            # Get actual winner from txt
            actual_winner = get_actual_winner(str(txt_path))
            
            if actual_winner is None:
                skipped_no_winner += 1
                print(f"[SKIP] {item.name}: No winner found in txt")
                continue
            
            # Load keypoints from Excel
            left_xdata, left_ydata, right_xdata, right_ydata = load_keypoints_from_excel(str(excel_path))
            
            # Parse phrase from txt
            phrase_fps = infer_phrase_fps(item, txt_path=txt_path)
            phrase = parse_txt_file(
                str(txt_path),
                fps=phrase_fps,
                video_path=str(video_path) if video_path is not None else None,
            )
            if left_xdata and 16 in left_xdata:
                max_frame = len(left_xdata[16]) - 1
                debug_referee_module._trim_phrase_to_frames(phrase, max_frame)
            side_hit_events = extract_side_hit_events(
                str(txt_path),
                fps=phrase.fps,
                video_path=str(video_path) if video_path is not None else None,
            )
            # Load normalization constant from JSON if available
            norm_constant = None
            if json_path.exists():
                try:
                    with open(json_path, 'r') as f:
                        old_data = json.load(f)
                        norm_constant = old_data.get("normalisation_constant")
                except:
                    pass
            
            # Run referee decision (now includes arm extension logic)
            decision = referee_decision(
                phrase,
                left_xdata, left_ydata,
                right_xdata, right_ydata,
                normalisation_constant=norm_constant,
                side_hit_events=side_hit_events,
            )
            raw_blade_relevant = has_relevant_blade_contact(phrase, side_hit_events)
            judged_with_blade_rules = uses_blade_contact_rules(decision)
            
            predicted_winner = decision.get("winner")
            
            # Update JSON with new decision
            result_data = {}
            if json_path.exists():
                try:
                    with open(json_path, 'r') as f:
                        result_data = json.load(f)
                except:
                    pass
            
            result_data["winner"] = predicted_winner
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
            
            # Sanitize and save
            result_data = sanitize_for_json(result_data)
            with open(json_path, 'w') as f:
                json.dump(result_data, f, indent=2)
            
            # Compare with actual winner
            total_checked += 1
            
            if not prediction_matches(actual_winner, predicted_winner):
                # Mismatch found!
                mismatches_found += 1
                
                # Copy entire folder to mismatched_results
                destination_root = mismatch_blade_dir if judged_with_blade_rules else mismatch_other_dir
                destination = destination_root / item.name
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(str(item), str(destination))
                if judged_with_blade_rules:
                    bucket_counts["mismatch_blade"] += 1
                else:
                    bucket_counts["mismatch_no_blade"] += 1
                
                print(f"[MISMATCH] {item.name}")
                print(f"  Actual: {actual_winner}, Predicted: {predicted_winner}")
                print(f"  Bucket: {'blade_contact' if judged_with_blade_rules else 'no_blade_contact'}")
                print(f"  Reason: {decision.get('reason')}")
                print()
            else:
                # Match found - copy to correct_results
                destination_root = correct_blade_dir if judged_with_blade_rules else correct_other_dir
                destination = destination_root / item.name
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(str(item), str(destination))
                if judged_with_blade_rules:
                    bucket_counts["correct_blade"] += 1
                else:
                    bucket_counts["correct_no_blade"] += 1
                
                match_label = actual_winner
                if actual_winner == 'abstain' and predicted_winner is not None:
                    match_label = f"abstain (predicted {predicted_winner})"
                print(
                    f"[MATCH] {item.name}: {match_label} ✓ "
                    f"({'blade_contact' if judged_with_blade_rules else 'no_blade_contact'})"
                )
        
        except Exception as e:
            errors += 1
            print(f"[ERROR] {item.name}: {e}")
        finally:
            processed += 1
            print()
    
    print("=" * 80)
    print(f"\n=== SUMMARY ===")
    print(f"Total folders processed: {total_processed}")
    print(f"Skipped (no Excel): {skipped_no_excel}")
    print(f"Skipped (no winner in txt): {skipped_no_winner}")
    print(f"Total checked: {total_checked}")
    print(f"Matches: {total_checked - mismatches_found}")
    print(f"Mismatches: {mismatches_found}")
    print(f"Errors: {errors}")
    print(f"Correct / blade_contact: {bucket_counts['correct_blade']}")
    print(f"Correct / no_blade_contact: {bucket_counts['correct_no_blade']}")
    print(f"Mismatch / blade_contact: {bucket_counts['mismatch_blade']}")
    print(f"Mismatch / no_blade_contact: {bucket_counts['mismatch_no_blade']}")
    print(f"\nAccuracy: {((total_checked - mismatches_found) / total_checked * 100):.2f}%" if total_checked > 0 else "N/A")
    print(f"\nCorrect results copied to: {correct_dir}")
    print(f"Mismatched results copied to: {mismatch_dir}")

if __name__ == "__main__":
    main()
