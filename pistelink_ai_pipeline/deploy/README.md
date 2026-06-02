# PisteLink AI Deployment Notes

This directory contains deployment assets for the AI-side service. The partner
PisteLink backend remains a separate service and owns MCU serial, audio,
`json.txt`, Web UI, and upload.

## Runtime Contract

- AI socket: `/run/pistelink/ai.sock`
- Match root: `/var/lib/pistelink/matches`
- AI writes video and intermediate artifacts under each match directory.
- Backend writes and later backfills `json.txt`.
- Backend result timeout should be `30` seconds for first strip tests.
- Analyzer startup timeout should be `120` seconds on Jetson cold boot. This
  only applies before `camera_ready`; per-touch AI result timeout remains the
  backend's `30` seconds.

## Install Service

Review `systemd/pistelink-ai.service` before installing. If the device uses the
partner default `nvidia` user, change `User`, `Group`, and paths accordingly.

```bash
sudo cp /home/thomas/fencing/pistelink_ai_pipeline/deploy/systemd/pistelink-ai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pistelink-ai.service
```

Then start or restart the partner backend service:

```bash
sudo systemctl restart pistelink.service
```

Check:

```bash
systemctl status pistelink-ai.service
systemctl status pistelink.service
curl -fsS http://127.0.0.1:8080/healthz
```

`ai` should be `ok` after the backend connects. `serial` can remain `error`
until the MCU is connected.
