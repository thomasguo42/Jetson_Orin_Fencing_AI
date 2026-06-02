# Jetson Orin Nano Fencing Bundle

This folder contains the client-side files needed to run the Arduino scoring box from a Jetson Orin Nano, plus the Arduino firmware project in case you want to reflash from the Jetson later.

## Included

- `control_fencing.py`: main scoring-box UI and camera recorder
- `judge_client.py`: HTTP upload helper used by `control_fencing.py`
- `run_control_fencing.sh`: small launcher with Jetson-friendly defaults
- `pip_requirements.txt`: pip packages used by the Python client
- `platformio.ini`, `src/`, `include/`, `lib/`, `test/`: Arduino firmware project

## Streaming Docs

Detailed docs for the current Jetson-local streaming analyzer path:

- English: [`docs/STREAMING_LOCAL_ANALYZER_EN.md`](docs/STREAMING_LOCAL_ANALYZER_EN.md)
- 中文: [`docs/STREAMING_LOCAL_ANALYZER_ZH.md`](docs/STREAMING_LOCAL_ANALYZER_ZH.md)

## Jetson Setup

Install system packages first:

```bash
sudo apt update
sudo apt install -y python3-pip python3-tk python3-opencv ffmpeg espeak
python3 -m pip install -r pip_requirements.txt
```

If you want to flash the Arduino from the Jetson too:

```bash
python3 -m pip install platformio
```

## Serial Port

The bundled client defaults to:

```bash
FENCING_SERIAL_PORT=/dev/ttyACM0
```

If your Arduino appears on a different device, override it when launching:

```bash
FENCING_SERIAL_PORT=/dev/ttyUSB0 ./run_control_fencing.sh
```

To find the device:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

If serial access is denied:

```bash
sudo usermod -a -G dialout $USER
```

Then log out and log back in.

## Camera

The launcher defaults to camera index `0`. Override it if needed:

```bash
FENCING_CAMERA_INDEX=1 ./run_control_fencing.sh
```

Useful camera overrides:

```bash
FENCING_CAMERA_WIDTH=1280
FENCING_CAMERA_HEIGHT=720
FENCING_CAMERA_FPS=30
```

## Judge Server

If you want uploads enabled, set:

```bash
export REFEREE_SERVER_URL=http://YOUR_SERVER_IP:8765/judge
export REFEREE_SEND_TO_SERVER=true
```

If you want Jetson-local/manual mode only:

```bash
export REFEREE_SEND_TO_SERVER=false
```

## Run

From inside this folder:

```bash
./run_control_fencing.sh
```

Or:

```bash
python3 control_fencing.py
```

## Notes

- The app creates a local `recordings/` folder automatically.
- `control_fencing.py` is a Tkinter GUI, so the Jetson needs a desktop/X session.
- If the Arduino is already flashed, you do not need to use the PlatformIO files.
