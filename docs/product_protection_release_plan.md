# PisteLink Product Protection And Release Plan

This document records the current release direction after auditing the repo.
It is intentionally conservative: first define the release boundary, then clean
and package, then enable encryption and secure boot on production hardware.

## Decisions

- Product runtime is `pistelink` plus `pistelink_ai_pipeline`.
- Customer units should not expose SSH.
- Live referee/judging must work fully offline.
- Wi-Fi and SFTP are allowed for upload, update, and non-live workflows.
- Protect backend, frontend, AI, analyzer, referee, and model assets strictly.
- Keep `blade_touch_referee_model.joblib`.
- Keep `.pt` and `.onnx` debug fallbacks only in internal/service builds unless a field requirement proves they are needed in customer builds.
- Do not ship Arduino firmware source in customer images.
- Prefer a production Jetson module with encrypted NVMe/rootfs over a removable SD-card product image.
- Use `/opt/pistelink` as the installed product root, with `/var/lib/pistelink` for match data and `/run/pistelink` for runtime sockets.

## Threat Model

Primary threats:

- Customer or competitor removes storage and reads files from another machine.
- Customer gets local shell access and copies source/model assets.
- Customer modifies code/model files to bypass licensing or alter judging.
- Remote attacker reaches update, upload, or web surfaces.
- Stolen device reveals source code, model files, credentials, or match data.

Non-goals:

- Prevent all reverse engineering by a well-funded lab with physical access to a running device.
- Protect secrets after a fully privileged runtime compromise.

Target outcome:

- Offline storage theft should not reveal source, models, credentials, or match data.
- Unsigned software should not boot.
- Customer-visible filesystem should not contain the development repo or plain core source.
- Updates should be signed and recoverable.

## Recommended Architecture

### Hardware And Storage

- Use production Jetson Orin Nano/NX module hardware for customer units.
- Prefer NVMe or eMMC rootfs over removable SD card.
- Use encrypted rootfs and encrypted writable data partitions.
- Keep `/boot` only as required by Jetson boot flow; do not store sensitive code, models, or keys there.

### Boot Chain

- Enable NVIDIA Secure Boot only after test units repeatedly pass flashing, boot, update, and recovery validation.
- Use production PKC/SBK/OEM keys generated on an offline secure machine.
- Treat fuse programming as irreversible manufacturing, not development.
- Use signed bootloader, kernel, DTB, initrd, and update payloads.
- Add rollback protection after the OTA strategy is proven.

NVIDIA's Jetson Linux documentation states that Secure Boot applies to Orin Nano/NX/AGX Orin and uses the BootROM chain of trust with fused PKC keys. It also warns that fuse bit values cannot be changed back once programmed, and production security mode blocks additional fuse writes. See:

- https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Security/SecureBoot.html

### Disk Encryption

- Use Jetson-supported LUKS disk encryption for rootfs and match data.
- Use OP-TEE/EKB/device-derived key flow so the unlock material is not stored plainly on disk.
- Validate by removing storage and confirming product code, models, config, and match data are unreadable.

NVIDIA's Jetson Linux disk encryption docs describe `APP_ENC` as the encrypted root partition, `cryptsetup`/LUKS unlock, and `luks-srv` in the trusted world deriving per-device passphrases. See:

- https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Security/DiskEncryption.html

### Runtime Layout

Use one product root:

```text
/opt/pistelink/
  backend/
  frontend/dist/
  sound/
  ai/
    service/
    analyzer/
    models/
  deploy/
```

Writable runtime/data:

```text
/var/lib/pistelink/
  matches/
  upload_pending.json

/run/pistelink/
  ai.sock
```

Service users:

```text
pistelink-backend
pistelink-ai
```

## Release Manifest

### Keep: Backend Product Runtime

```text
pistelink/backend/
pistelink/frontend/dist/
pistelink/sound/
pistelink/requirements.txt
pistelink/deploy/
pistelink/run_pistelink_backend.sh
```

Notes:

- Keep backend tests in the development repo, not in customer images.
- Runtime config should be generated or installed under `/etc/pistelink/config.toml`.
- Remove fixed customer IPs and secrets from templates before general release.
- Replace `known_hosts=None` SFTP behavior with host key pinning.

### Keep: AI Product Runtime

```text
pistelink_ai_pipeline/deploy/
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_ai_service.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_analysis_adapter.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_camera_recorder.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_protocol.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_signal_adapter.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/local_streaming_manager.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/control_fencing.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/run_pistelink_ai_service.sh
pistelink_ai_pipeline/jetson_orin_nano_bundle/pip_requirements.txt
```

Notes:

- `control_fencing.py` is currently needed because `PisteLinkCameraRecorder` uses `control_fencing.CameraRecorder`.
- Long term, extract camera recording into a smaller product module to avoid shipping the old GUI/referee code path.

### Keep: Analyzer Runtime

```text
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/requirements.txt
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/install_pi.sh
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/run_phrase_pipeline.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/__init__.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/debug_referee_fps30.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/reprocess_phrase_limb_interp_jumpsafe_experimental.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/reprocess_phrase_worker.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/blade_touch_referee.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/classify_accident_contact.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/classify_blade_contact_benefit.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/src/
```

Optional dev/internal tools:

```text
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/replay_live_stream_analysis.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/run_limb_interp_batch.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/run_limb_interp_jumpsafe_batch.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/validate_all_results_fps30.py
```

Do not include optional tools in customer builds unless needed for signed support mode.

### Keep: Product Model Assets

Primary runtime:

```text
/opt/pistelink/ai/models/yolo26l-pose_fast_fp16_ultra.engine
/opt/pistelink/ai/models/blade_touch_referee_model.joblib
```

Source locations:

```text
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/experiments/yolov8_pose/matrix_all_20260404/yolo26l-pose/yolo26l-pose_fast_fp16_ultra.engine
portable_fencing_pipeline/results/blade_touch_referee_model.joblib
```

Debug/internal fallback only:

```text
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/yolo26s-pose.pt
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/yolo26s-pose.onnx
```

Customer release default:

- Ship the TensorRT engine and blade model.
- Do not ship `.pt` or `.onnx` unless enabled by a separate internal/support build flag.
- Never ship training data, model sweep outputs, overlays, or benchmark videos.

### Archive: Older Or Duplicate Code

Move to an archive area in the development repo, not the customer image:

```text
portable_fencing_pipeline/
portable_fencing_pipeline_low_latency/
portable_fencing_pipeline_low_latency_streaming/
jetson_orin_nano_bundle/
pistelink_partner_mock_ai/
trt_pose/
torch2trt/
```

Notes:

- Before archiving, copy `portable_fencing_pipeline/results/blade_touch_referee_model.joblib` into the product model asset path.
- Keep `pistelink_partner_mock_ai` only for partner/backend protocol tests.
- `trt_pose` and `torch2trt` are third-party/legacy experiment repos, not current runtime dependencies.

### Exclude From Customer Images

```text
.git/
.agents/
.codex/
.pytest_cache/
**/.pytest_cache/
**/.venv/
**/__pycache__/
**/*.pyc
**/logs/
**/debug.txt
**/recordings/
**/runtime_inputs/
**/runtime_outputs/
**/experiments/
**/model_variants/
**/.vendor/
**/*.mp4
**/*.avi
**/*.onnx
**/*.pt
**/*.7z
pistelink/deploy/Miniforge3-Linux-aarch64.sh
pistelink_ai_pipeline/jetson_orin_nano_bundle/platformio.ini
pistelink_ai_pipeline/jetson_orin_nano_bundle/src/
pistelink_ai_pipeline/jetson_orin_nano_bundle/include/
pistelink_ai_pipeline/jetson_orin_nano_bundle/lib/
pistelink_ai_pipeline/jetson_orin_nano_bundle/test/
```

Allowlist exceptions:

- One approved `.engine` model.
- Optional `.pt/.onnx` only in an internal/support build, not standard customer release.
- Frontend built assets under `pistelink/frontend/dist/assets/`.

## Required Code Fixes Before Packaging

1. Remove hardcoded Gemini API key from all pipeline copies and rotate the exposed key.
2. Remove `/home/thomas/fencing/...` fallbacks from production-adjacent code.
3. Make systemd units install-root based.
4. Replace model symlinks with direct product model paths.
5. Pin SFTP host keys instead of disabling host key verification.
6. Remove fixed customer SFTP IPs from generic config templates.
7. Add startup integrity verification for model and compiled code artifacts.
8. Extract `CameraRecorder` from `control_fencing.py`, or clearly mark `control_fencing.py` as camera-only in product builds.

Known current findings:

```text
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/src/referee/analysis.py
  contains a hardcoded GEMINI_API_KEY fallback.

pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_analysis_adapter.py
  contains a /home/thomas/fencing analyzer Python fallback.

pistelink_ai_pipeline/jetson_orin_nano_bundle/control_fencing.py
  contains a /home/thomas/fencing analyzer root fallback.

pistelink/backend/uploader.py
  uses known_hosts=None for SFTP.
```

## Code Protection Plan

### Build Types

Development build:

- Plain source allowed.
- Tests, mocks, and debug fallbacks allowed.
- No secure boot fusing.

Internal service build:

- Compiled/packaged code.
- Debug fallback models may be present.
- SSH disabled by default, but service access may be enabled by a signed local procedure.
- Detailed logs available.

Customer production build:

- No plain core source for backend, AI, analyzer, or referee logic.
- No SSH.
- No compilers, git, dev tools, tests, or mocks.
- Read-only code/model partition.
- Encrypted rootfs/data.
- Secure boot enabled.
- Signed updates only.

### Compilation Strategy

Compile or package at least:

```text
pistelink/backend/*.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/pistelink_*.py
pistelink_ai_pipeline/jetson_orin_nano_bundle/local_streaming_manager.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/debug_referee_fps30.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/scripts/reprocess_phrase_limb_interp_jumpsafe_experimental.py
pistelink_ai_pipeline/portable_fencing_pipeline_low_latency_streaming/src/referee/analysis.py
```

Candidate approaches:

- Nuitka for standalone binary/native extension packaging.
- Cython for selected sensitive modules.
- PyInstaller is easier but less protective than Nuitka/Cython.

Recommendation:

- Use Nuitka first for the AI/analyzer service path.
- Use Cython or Nuitka selectively for backend modules if backend packaging becomes complicated.
- Leave only small shell/systemd launchers and non-sensitive config templates in plain text.

## OS Hardening Plan

Systemd service hardening targets:

```ini
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=false
ReadWritePaths=/var/lib/pistelink /run/pistelink
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
MemoryDenyWriteExecute=false
```

Notes:

- `PrivateDevices=false` may be needed for camera, serial, audio, and GPU access.
- `MemoryDenyWriteExecute=true` can break Python/native ML runtimes; test before enabling.
- AI service needs `video`, `render`, possibly `audio` groups.
- Backend service needs `dialout` and `audio`.

Network hardening:

- Disable SSH.
- Bind backend HTTP to localhost unless kiosk/proxy design requires otherwise.
- If remote management is needed, use signed update/check-in protocol, not shell access.
- SFTP upload must use pinned host keys and least-privilege upload account.

## Update Strategy

Required:

- Signed update packages.
- Update manifest with version, hash list, and rollback policy.
- A/B rootfs or reliable recovery partition before production fuse burn.
- Health check before marking update successful.
- Ability to export support logs without shell access.

Recommended:

- Separate update channels: development, internal service, production.
- Production devices accept only production-signed packages.
- Keep production signing keys offline.

## Risks And Downsides

### Encrypted NVMe/rootfs

Benefits:

- Strongest practical protection against removed-storage theft.
- Keeps code, models, config, and match data unreadable offline.
- Better fit than removable SD for a commercial product.

Downsides:

- More complex flashing and manufacturing.
- Bad initrd/crypttab/key setup can make a device fail to boot.
- Field recovery requires a planned recovery flow.
- Encryption does not protect code after the device is booted and services are running.
- Storage failure or corruption recovery is harder.

Decision:

- Use encrypted NVMe/rootfs for production.
- Keep unencrypted dev images for engineering only.

### Secure Boot And Fuses

Benefits:

- Prevents unsigned boot chain and OS tampering.
- Pairs with disk encryption so attackers cannot boot a modified image to extract secrets.

Downsides:

- Fuse programming is irreversible.
- Wrong fuse/key flow can permanently lock or brick devices.
- Kernel/initrd/bootloader updates must be signed correctly.
- Debugging becomes harder after production mode.
- Manufacturing key custody becomes a serious operational process.

Decision:

- Do not burn fuses during early cleanup.
- Use sacrificial test units first.
- Burn production fuses only after signed update and recovery are proven.

### No SSH

Benefits:

- Removes a major support and attack surface.
- Prevents customers from casually copying code from a shell.

Downsides:

- Support/debugging is harder.
- Need UI or local export for logs, update status, health checks, and diagnostics.

Decision:

- Disable SSH in customer builds.
- Build a signed support bundle/export workflow.

### Compiled Python

Benefits:

- Raises reverse-engineering cost.
- Avoids shipping readable core algorithms.

Downsides:

- Not perfect protection.
- Can complicate packaging and dependency handling.
- Stack traces/debugging are less convenient.
- Native builds can be architecture and JetPack specific.

Decision:

- Compile the core backend, AI, analyzer, and referee logic for customer builds.
- Keep a plain-source dev build for engineering.

### Debug Fallback Models

Benefits:

- Useful for internal diagnosis if TensorRT engine fails.

Downsides:

- `.pt` and `.onnx` reveal more model structure than `.engine`.
- They increase attack surface and image size.

Decision:

- Customer release ships `.engine` only.
- `.pt/.onnx` are internal/service-build assets unless explicitly required.

## Next Implementation Steps

1. Create product release manifest tooling that copies only allowlisted files into a staging directory.
2. Copy `blade_touch_referee_model.joblib` into the product model asset path.
3. Remove secret literals and rotate the exposed Gemini key.
4. Replace dev paths with `/opt/pistelink` paths.
5. Replace model symlink with direct `/opt/pistelink/ai/models/...` path.
6. Add SFTP host key pinning.
7. Add systemd hardening.
8. Add CI or local checks that fail if excluded paths appear in the release stage.
9. Build and run a non-secure staged product image.
10. Add code compilation.
11. Add rootfs/data encryption.
12. Add signed update flow.
13. Enable secure boot on test devices.
14. Finalize manufacturing fuse burn process.

## Open Concerns

- We still need the exact Jetson module/storage SKU for production image design.
- We need a recovery/update strategy before secure boot fuse burn.
- We need a no-SSH support workflow.
- We need to test whether `blade_touch_referee_model.joblib` changes decision quality after restoring it.
- We need to confirm whether the frontend source exists elsewhere, because this checkout only contains built frontend assets.
