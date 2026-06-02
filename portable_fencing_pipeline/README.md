# Portable Fencing Pipeline

This folder is a self-contained Raspberry Pi bundle for the current end-to-end pipeline:

- fisheye correction
- YOLO26 pose extraction
- manual TXT indices, then SAM3, then auto initialization
- jump-safe base tracking
- base interpolation with max internal gap `5`
- narrow limb interpolation repair
- repaired keypoint Excel and overlay
- FPS30 judging on the repaired keypoints

## Folder Layout

- `run_phrase_pipeline.py`: one phrase end-to-end
- `run_batch_pipeline.py`: batch end-to-end
- `scripts/`: copied pipeline/judging scripts
- `src/`: copied shared extraction modules
- `models/`: YOLO26 weights and offline SAM3 snapshot
- `results/blade_touch_referee_model.joblib`: blade-contact model used by FPS30 judging
- `data/training_data/...xlsx`: bundled reference init keypoints asset
- `runtime_inputs/`: default place to put phrase folders on the Pi
- `runtime_outputs/`: default place where outputs are written
- `logs/`: debug and batch logs

## Install

```bash
cd portable_fencing_pipeline
./install_pi.sh
```

Install system video tools too:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

Activate the virtual environment before running:

```bash
source .venv/bin/activate
```

## Input Format

Each phrase folder should contain:

- one input video (`.avi` or `.mp4`)
- one phrase `.txt`

Put phrase folders under `runtime_inputs/`, or point the runners to another directory.

## Run One Phrase

```bash
python run_phrase_pipeline.py --phrase-dir runtime_inputs/PHRASE_FOLDER
```

Optional:

```bash
python run_phrase_pipeline.py \
  --phrase-dir runtime_inputs/PHRASE_FOLDER \
  --progress \
  --yolo-verbose
```

Outputs go to:

`runtime_outputs/experimental_limb_interp_jumpsafe/PHRASE_FOLDER/`

Key files:

- `*_corrected.mp4`
- `*_yolo_all_people_overlay.mp4`
- `*_limb_interp_keypoints.xlsx`
- `*_limb_interp_overlay.mp4`
- `analysis_result_limb_interp_experimental.json`
- `analysis_result.json` (final FPS30 judging result)

If SAM is used, the folder also includes:

- `sam3_first_frame.png`
- `sam3_fencer_overlay.png`
- `sam3_fencer_union.png`
- `sam3_fencer_metadata.json`

## Run A Batch

```bash
python run_batch_pipeline.py --base-dir runtime_inputs
```

Optional:

```bash
python run_batch_pipeline.py \
  --base-dir runtime_inputs \
  --skip-existing \
  --start-after PHRASE_NAME \
  --limit 20 \
  --progress
```

## Repair Only

If a phrase folder already has extracted keypoints and corrected video, you can skip re-extraction:

```bash
python run_phrase_pipeline.py --phrase-dir runtime_inputs/PHRASE_FOLDER --repair-only
```

## Notes

- The bundle is offline-ready for SAM3 because `models/sam3/` is included.
- The FPS30 judge writes debug output to `logs/debug.txt`.
- The extraction script inside the bundle is `scripts/reprocess_phrase_limb_interp_jumpsafe_experimental.py`.
- The FPS30 validator remains available as `scripts/validate_all_results_fps30.py` if you want to rerun judging over an existing output root.
