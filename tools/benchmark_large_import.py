from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

import httpx


MIB = 1024 * 1024
GIB = 1024 * MIB
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(base_url: str, timeout: float = 15) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if httpx.get(f"{base_url}/api/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("benchmark server did not start")


def run(payload_mib: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ai-checklist-large-import-") as temporary:
        root = Path(temporary)
        os.environ["DATABASE_URL"] = f"sqlite:///{(root / 'benchmark.db').as_posix()}"
        os.environ["LOCALAPPDATA"] = str(root)

        import uvicorn
        from app.core.database import engine
        from app.main import app

        port = free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"
        wait_for_server(base_url)

        text_bytes = payload_mib * MIB
        video_bytes = 47 * GIB
        started_at = time.perf_counter()
        tracemalloc.start()
        try:
            with httpx.Client(base_url=base_url, timeout=180) as client:
                init_at = time.perf_counter()
                response = client.post("/api/import/uploads", json={
                    "root_name": "50GB-large-import-benchmark",
                    "source_kind": "directory",
                    "files": [
                        {"relative_path": "course/large-notes.txt", "size": text_bytes, "mime_type": "text/plain"},
                        {"relative_path": "course/47GB-video.mp4", "size": video_bytes, "mime_type": "video/mp4", "duration_seconds": 8 * 3600},
                    ],
                })
                response.raise_for_status()
                batch = response.json()
                init_seconds = time.perf_counter() - init_at
                text_doc, video_doc = batch["documents"]
                cache_root = root / "AI Checklist Hub" / "upload-cache" / str(batch["id"])
                media_was_copied = any(cache_root.glob(f"{video_doc['id']}.*")) if cache_root.exists() else False

                chunk = ("学习资料：章节、练习与复习。\n".encode("utf-8") * (8 * MIB // 40 + 1))[:8 * MIB]
                upload_at = time.perf_counter()
                offset = 0
                while offset < text_bytes:
                    current = chunk[: min(len(chunk), text_bytes - offset)]
                    part = client.put(
                        f"/api/import/uploads/{batch['id']}/{text_doc['id']}/chunk",
                        params={"offset": offset}, content=current,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    part.raise_for_status()
                    offset = int(part.json()["received"])
                upload_seconds = time.perf_counter() - upload_at
                cached_bytes = (cache_root / f"{text_doc['id']}.part").stat().st_size

                extract_at = time.perf_counter()
                extracted = client.post(f"/api/import/uploads/{batch['id']}/{text_doc['id']}/complete")
                extracted.raise_for_status()
                completed = client.post(f"/api/import/uploads/{batch['id']}/complete")
                completed.raise_for_status()
                extract_seconds = time.perf_counter() - extract_at
                result = completed.json()
        finally:
            _, peak_python_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            server.should_exit = True
            thread.join(timeout=10)
            engine.dispose()

        total_seconds = time.perf_counter() - started_at
        throughput = payload_mib / upload_seconds
        projected_3gb_seconds = 3072 / max(throughput, 0.001) + extract_seconds * (3072 / payload_mib)
        return {
            "scenario": {"text_declared_gib": round(text_bytes / GIB, 3), "video_declared_gib": 47, "total_declared_gib": round((text_bytes + video_bytes) / GIB, 3)},
            "manifest_init_seconds": round(init_seconds, 3),
            "video_metadata_only": video_doc["metadata"].get("metadata_only") is True,
            "video_binary_copied": media_was_copied,
            "actual_text_transferred_mib": payload_mib,
            "upload_seconds": round(upload_seconds, 3),
            "upload_throughput_mib_s": round(throughput, 1),
            "extract_and_hash_seconds": round(extract_seconds, 3),
            "end_to_end_seconds": round(total_seconds, 3),
            "projected_3gb_text_seconds_at_measured_rate": round(projected_3gb_seconds, 1),
            "peak_python_tracemalloc_mib": round(peak_python_bytes / MIB, 1),
            "peak_cache_mib": round(cached_bytes / MIB, 1),
            "batch_status": result["status"],
            "supported_count": result["supported_count"],
            "cache_removed_after_completion": not cache_root.exists(),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark bounded-memory large import over real localhost HTTP.")
    parser.add_argument("--payload-mib", type=int, default=512, choices=range(8, 4097, 8), metavar="8..4096")
    args = parser.parse_args()
    print(json.dumps(run(args.payload_mib), ensure_ascii=False, indent=2))
