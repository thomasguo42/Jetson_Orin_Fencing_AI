#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import time
from pathlib import Path


MODELS = [
    "yolov8s-pose.pt",
    "yolov8m-pose.pt",
    "yolov8l-pose.pt",
    "yolo11s-pose.pt",
    "yolo11m-pose.pt",
    "yolo11l-pose.pt",
    "yolo26s-pose.pt",
    "yolo26m-pose.pt",
    "yolo26l-pose.pt",
]


def run(cmd, env=None, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return result


def uses_end2end_tensor_output(model_name: str) -> bool:
    return Path(model_name).name.startswith("yolo26")


def parse_trtexec_benchmark(stdout: str) -> dict:
    throughput = re.search(r"Throughput:\s+([0-9.]+)\s+qps", stdout)
    latency = re.search(r"Latency:.*mean = ([0-9.]+) ms", stdout)
    enqueue = re.search(r"Enqueue Time:.*mean = ([0-9.]+) ms", stdout)
    gpu = re.search(r"GPU Compute Time:.*mean = ([0-9.]+) ms", stdout)
    return {
        "throughput_qps": float(throughput.group(1)) if throughput else None,
        "latency_mean_ms": float(latency.group(1)) if latency else None,
        "enqueue_mean_ms": float(enqueue.group(1)) if enqueue else None,
        "gpu_compute_mean_ms": float(gpu.group(1)) if gpu else None,
        "stdout_tail": stdout[-4000:],
    }


def parse_trtexec_build(stdout: str) -> dict:
    gen = re.search(r"Engine generation completed in ([0-9.]+) seconds", stdout)
    built = re.search(r"Engine built in ([0-9.]+) sec", stdout)
    size = re.search(r"Created engine with size: ([0-9.]+) MiB", stdout)
    return {
        "generation_seconds": float(gen.group(1)) if gen else None,
        "build_seconds": float(built.group(1)) if built else None,
        "engine_size_mib": float(size.group(1)) if size else None,
        "stdout_tail": stdout[-4000:],
    }


def main():
    parser = argparse.ArgumentParser(description="Run a TensorRT comparison matrix for YOLO pose models.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=512)
    parser.add_argument("--conf", type=float, default=0.15)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--models", nargs="*", default=MODELS)
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    parser.add_argument("--trtexec", type=Path, default=Path("/usr/src/tensorrt/bin/trtexec"))
    parser.add_argument("--force", action="store_true", help="Rebuild artifacts even if cached outputs exist.")
    args = parser.parse_args()

    repo_root = Path("/home/thomas/fencing")
    exp_root = Path("/home/thomas/fencing/portable_fencing_pipeline_low_latency/experiments/yolov8_pose")
    base_pythonpath = (
        "/usr/lib/python3.10/dist-packages:"
        "/home/thomas/fencing/portable_fencing_pipeline_low_latency/.venv/lib/python3.10/site-packages"
    )
    export_pythonpath = (
        base_pythonpath
        + ":"
        + "/home/thomas/fencing/portable_fencing_pipeline_low_latency/model_variants/yolov8_pose/.vendor"
    )
    env = dict(subprocess.os.environ)
    env["PYTHONPATH"] = base_pythonpath
    export_env = dict(env)
    export_env["PYTHONPATH"] = export_pythonpath

    args.output_root.mkdir(parents=True, exist_ok=True)
    timing_cache = args.output_root / "trt_global.cache"
    aggregate = []

    for model_name in args.models:
        stem = Path(model_name).stem
        run_dir = args.output_root / stem
        run_dir.mkdir(parents=True, exist_ok=True)
        print(f"[matrix] starting {model_name}", flush=True)
        model_log = {
            "model": model_name,
            "status": "started",
        }

        try:
            pt_candidate = repo_root / model_name
            if pt_candidate.exists():
                pt_path = pt_candidate
            else:
                download_cmd = [
                    str(args.python),
                    "-c",
                    (
                        "from ultralytics import YOLO; "
                        f"m=YOLO('{model_name}'); "
                        "print(m.ckpt_path)"
                    ),
                ]
                r = run(download_cmd, env=env, cwd=repo_root)
                if r.returncode != 0:
                    raise RuntimeError(f"Checkpoint download/load failed:\n{r.stderr}")
                pt_path = Path(r.stdout.strip().splitlines()[-1])

            onnx_path = run_dir / f"{stem}_{args.imgsz}.onnx"
            if args.force or not onnx_path.exists():
                onnx_cmd = [
                    str(args.python),
                    "-c",
                    (
                        "from ultralytics import YOLO; "
                        f"m=YOLO(r'{pt_path}'); "
                        f"p=m.export(format='onnx', imgsz={args.imgsz}, half=False, device=0, verbose=False, simplify=False); "
                        "print(p)"
                    ),
                ]
                r = run(onnx_cmd, env=export_env, cwd=run_dir)
                if r.returncode != 0:
                    raise RuntimeError(f"ONNX export failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
                exported_onnx = Path(r.stdout.strip().splitlines()[-1])
                if exported_onnx != onnx_path:
                    onnx_path.write_bytes(exported_onnx.read_bytes())

            raw_engine = run_dir / f"{stem}_fast_fp16.engine"
            ultra_engine = run_dir / f"{stem}_fast_fp16_ultra.engine"
            build_info = None
            if args.force or not raw_engine.exists():
                build_cmd = [
                    str(args.trtexec),
                    f"--onnx={onnx_path}",
                    f"--saveEngine={raw_engine}",
                    "--fp16",
                    "--memPoolSize=workspace:1024",
                    "--avgTiming=1",
                    "--builderOptimizationLevel=0",
                    "--skipInference",
                    f"--timingCacheFile={timing_cache}",
                ]
                t0 = time.perf_counter()
                r = run(build_cmd, cwd=repo_root)
                build_wall = time.perf_counter() - t0
                if r.returncode != 0 or not raw_engine.exists():
                    raise RuntimeError(f"TensorRT build failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
                build_info = parse_trtexec_build(r.stdout)
                build_info["wall_seconds"] = build_wall

            if args.force or not ultra_engine.exists():
                wrap_cmd = [
                    str(args.python),
                    str(exp_root / "wrap_engine_with_metadata.py"),
                    "--pt-model",
                    str(pt_path),
                    "--engine",
                    str(raw_engine),
                    "--output",
                    str(ultra_engine),
                    "--imgsz",
                    str(args.imgsz),
                ]
                if uses_end2end_tensor_output(model_name):
                    wrap_cmd.append("--end2end")
                r = run(wrap_cmd, env=env, cwd=repo_root)
                if r.returncode != 0 or not ultra_engine.exists():
                    raise RuntimeError(f"Metadata wrap failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")

            bench_cmd = [
                str(args.trtexec),
                f"--loadEngine={raw_engine}",
                "--useCudaGraph",
                "--noDataTransfers",
                f"--duration={args.duration}",
            ]
            r = run(bench_cmd, cwd=repo_root)
            if r.returncode != 0:
                raise RuntimeError(f"TensorRT benchmark failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
            trt_bench = parse_trtexec_benchmark(r.stdout)

            overlay_json = run_dir / f"{stem}_overlay_summary.json"
            overlay_video = run_dir / f"{stem}_overlay.mp4"
            overlay_cmd = [
                str(args.python),
                str(exp_root / "benchmark_video.py"),
                "--video",
                str(args.video),
                "--model",
                str(ultra_engine),
                "--output-json",
                str(overlay_json),
                "--overlay-video",
                str(overlay_video),
                "--imgsz",
                str(args.imgsz),
                "--conf",
                str(args.conf),
            ]
            t1 = time.perf_counter()
            r = run(overlay_cmd, env=env, cwd=repo_root)
            overlay_wall = time.perf_counter() - t1
            if r.returncode != 0:
                raise RuntimeError(f"Overlay export failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
            overlay_summary = json.loads(overlay_json.read_text())
            overlay_summary["wall_seconds"] = overlay_wall

            record = {
                "model": model_name,
                "status": "ok",
                "pt_path": str(pt_path),
                "onnx_path": str(onnx_path),
                "raw_engine": str(raw_engine),
                "ultra_engine": str(ultra_engine),
                "build": build_info,
                "trtexec_benchmark": trt_bench,
                "ultralytics_engine_summary": overlay_summary,
            }
            (run_dir / f"{stem}_record.json").write_text(json.dumps(record, indent=2))
            aggregate.append(record)
            print(f"[matrix] completed {model_name}", flush=True)
        except Exception as exc:
            model_log["status"] = "error"
            model_log["error"] = str(exc)
            (run_dir / f"{stem}_record.json").write_text(json.dumps(model_log, indent=2))
            aggregate.append(model_log)
            print(f"[matrix] failed {model_name}: {exc}", flush=True)

    (args.output_root / "matrix_summary.json").write_text(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
