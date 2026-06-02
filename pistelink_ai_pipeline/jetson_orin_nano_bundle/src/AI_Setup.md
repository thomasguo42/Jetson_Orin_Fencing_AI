# Remote AI Referee Pipeline Setup

This guide walks through deploying the fencing referee pipeline on a remote Linux VM and invoking it from another machine (for example, your laptop).

## 1. Repository Layout

Key entry points:
- `AI_Referee.py`: single-phrase pipeline and CLI. Accepts one `.avi` plus matching signal `.txt` and prints a JSON decision (optionally exports the keypoint Excel workbook).
- `referee_service.py`: FastAPI application exposing the pipeline over HTTP.
- `referee_client.py`: Convenience CLI for posting videos/signals to the service from a remote machine and optionally saving the returned Excel file.
- `training_data/`: sample inputs you can use for smoke testing.

## 2. Server Preparation

1. **System prerequisites**
   - Ubuntu 22.04 (or similar) with Python 3.10+
   - Optional GPU with CUDA drivers if you want hardware acceleration; the code also runs on CPU.

2. **Create a virtual environment (recommended)**
   ```bash
   python3 -m venv ~/ai-referee-venv
   source ~/ai-referee-venv/bin/activate
   python -m pip install --upgrade pip
   ```

3. **Install dependencies**
   ```bash
   pip install fastapi uvicorn[standard] ultralytics opencv-python-headless pandas numpy openpyxl tqdm requests
   ```
   - `ultralytics` will download the YOLO pose model the first time you run the app. To pre-download, run `python - <<'PY'
from ultralytics import YOLO
YOLO('yolov8m-pose.pt')
PY`.

4. **Model weights**
   - Default path is `yolov8m-pose.pt` in the working directory. If you keep it elsewhere, set `REFEREE_YOLO_MODEL=/path/to/weights.pt` before launching the service.

5. **Expose the HTTP port**
   - Decide on a port (default `8000`). Allow inbound traffic through your VM firewall / cloud security group. Example with `ufw`:
     ```bash
     sudo ufw allow 8000/tcp
     ```

## 3. Running the Referee Service

From the project root (inside the virtualenv):
```bash
export REFEREE_HOST=0.0.0.0      # listen on all interfaces
export REFEREE_PORT=8000         # choose any open port
python referee_service.py
```

The server loads YOLO once on startup and serves:
- `GET /health` – readiness probe
- `POST /analyze` – upload a single `.avi` & `.txt` pair for adjudication. Optional form fields:
  - `include_keypoints=true` to embed per-frame coordinates in the JSON
  - `include_excel=true` to include the keypoint workbook (base64) in the JSON

Use `CTRL+C` to stop the service. For production you can wrap it with a process manager (systemd, supervisord, Docker, etc.).

## 4. Optional: Local CLI on the Server

For quick testing without HTTP, run:
```bash
python AI_Referee.py path/to/phrase.avi path/to/phrase.txt \
    --include-keypoints \
    --include-excel-json \
    --excel-out keypoints.xlsx
```
- Output is JSON. Drop `--include-keypoints` for a concise decision and `--include-excel-json` if you only need the workbook file.
- `--excel-out` writes the Excel workbook locally; if omitted, the workbook is only embedded (base64) when `--include-excel-json` is provided.
- The command loads the YOLO weights on demand; subsequent runs can reuse them by keeping the interpreter alive (e.g., via the service).

## 5. Client Setup (Laptop)

1. Copy `referee_client.py` to your laptop.
2. Ensure Python 3.10+ and install `requests`:
   ```bash
   python3 -m pip install --user requests
   ```
3. Check server health:
```bash
python referee_client.py http://<vm-ip>:8000 --health
```
4. Send a phrase for adjudication and request the Excel workbook:
```bash
python referee_client.py \
    http://<vm-ip>:8000 \
    /path/to/phrase.avi \
    /path/to/phrase.txt \
    --include-keypoints \      # optional, large response
    --include-excel \          # embed Excel as base64
    --save-excel phrase_keypoints.xlsx
```
5. The client prints the JSON response. Capture it into a file if needed:
```bash
python referee_client.py http://<vm-ip>:8000 phrase.avi phrase.txt > decision.json
```
   - When `--save-excel` is provided, the workbook is saved locally and the JSON reflects the destination path.

## 6. Response Structure

Successful responses include:
- `status`: `success`
- `winner`: `"left"` or `"right"`
- `reason`: textual explanation of the call
- `frames_analyzed`, `normalisation_constant`, `video_angle`
- `phrase`: timings from the electric signal log (start, hit time, pauses)
- `left_pauses` / `right_pauses`: pause/retreat intervals with timing
- `blade_analysis`, `blade_details`, `speed_comparison`: blade/action metrics
- `processing_time_seconds`: core analysis time
- `wall_time_seconds`: end-to-end server wall clock time
- `keypoints`: present only when requested (large payload; per-frame coordinates)
- `excel_file`: when requested, holds `filename` and either `content_base64` (if you did not use `--save-excel`) or `saved_to` showing where the workbook was stored.

If the TXT file records only one valid hit, the service returns:
```
{
  "status": "skipped",
  "reason": "Only one fencer recorded a valid hit; nothing to adjudicate.",
  "declared_winner": "Right",  # from the electric signal file
  "phrase": { ... }
}
```

## 7. Operational Tips

- The first request after a restart can take longer due to YOLO warm-up.
- For repeated calls, keep the service alive; it shares the preloaded model across requests.
- If you need HTTPS, place a reverse proxy (nginx, Caddy, Traefik) in front of the FastAPI app.
- Monitor resource usage; CPU-only inference is slower, so batch requests accordingly.
- Back up the `.txt` electric signal files with their videos—they are required for a decision.

With these pieces in place, your laptop can upload fencing phrases to the VM, receive structured officiating decisions, and integrate the results into downstream tooling.
