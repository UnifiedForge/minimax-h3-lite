#!/usr/bin/env python3
"""
Emulates a lower-RAM Mac on THIS SAME machine by wiring (mlock) enough
memory to bring *free* RAM down to roughly what a machine with
TARGET_TOTAL_GB of total RAM would have, then fires a battery of real
generations at the already-running Lite server and records what happens
(success / error / timeout) plus the actual peak memory footprint each one
hit. Output is a JSON file you hand back for building the RAM-tier table.

This is NOT a perfect stand-in for a real smaller Mac (unified-memory
bandwidth and kernel behavior under genuine scarcity can differ from an
artificially wired block) - it's a first, honest approximation, good enough
for a *rough* first table. The real calibration comes later from actual
user telemetry across real hardware.

BEFORE RUNNING:
  1. The Lite server must already be running (run.sh or the launcher) -
     this script only talks to http://127.0.0.1:8766, it does not start
     the server itself.
  2. Close everything else you can (browser tabs, Slack, etc.) so the
     baseline free RAM is representative of a dedicated generation machine,
     not skewed by your own open apps.
  3. Keep Activity Monitor open while this runs. If the WHOLE Mac starts
     feeling unresponsive (not just h3 - Finder, menu bar, mouse cursor),
     Ctrl+C immediately. The MIN_FREE_GB floor below is a safety net, not
     a guarantee - macOS's mlock() has its own limits, and if "Free" in
     Activity Monitor doesn't actually drop by roughly the wired amount,
     the ballast isn't working as intended and the run isn't meaningful
     (Ctrl+C, note it, and try a smaller ballast).

USAGE:
    python3 ram_tier_test.py 24      # emulate a 24GB-total Mac
    python3 ram_tier_test.py 16      # emulate a 16GB-total Mac
    python3 ram_tier_test.py 32
    python3 ram_tier_test.py 48

Ctrl+C at any point releases the ballast and exits cleanly (results so far
are still written out). Writes ram_tier_results_<N>gb.json next to this
script.
"""
import ctypes
import ctypes.util
import json
import mmap
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8766"
MIN_FREE_GB = 6.0  # never emulate a target below this - matches the floor
                    # ram_ballast.py itself uses; leaving less than this free
                    # risks the OS itself (WindowServer, mouse/keyboard) hanging.
OUT_DIR = Path(__file__).parent

# Image mode = one 22-frame chunk, cheap and fast - the right tool for a
# broad first sweep. 3 resolution classes x 3 layer counts (draft/balanced/
# quality-equivalent). Video-mode confirmation runs are a deliberate second
# pass once this first table exists, not part of this script.
COMBOS = []
for w, h, res_label in [(512, 512, "sq512"), (768, 768, "sq768"), (768, 1344, "tall_max")]:
    for layers, preset_label in [(40, "draft"), (45, "balanced"), (50, "quality")]:
        COMBOS.append(dict(
            label=f"img_{res_label}_{preset_label}",
            width=w, height=h, mode="image",
            preset="custom", steps=20, layers=layers, reuse=2,
        ))


def total_ram_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
    return int(out.stdout.strip()) / (1024 ** 3)


def free_ram_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True)
    lines = out.stdout.splitlines()
    page = 4096
    if lines and "page size of" in lines[0]:
        try:
            page = int(lines[0].split("page size of")[1].split()[0])
        except Exception:
            pass
    stats = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if v.isdigit():
                stats[k.strip()] = int(v)
    free_pages = stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
    return free_pages * page / (1024 ** 3)


def api_get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def api_post_generate(combo: dict) -> str:
    boundary = "----ramtierboundary"

    def field(name, value):
        return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'

    body = field("prompt", f"ram tier test {combo['label']}")
    body += field("width", combo["width"])
    body += field("height", combo["height"])
    body += field("mode", combo["mode"])
    body += field("preset", combo["preset"])
    if combo["preset"] == "custom":
        body += field("steps", combo["steps"])
        body += field("layers", combo["layers"])
        body += field("reuse", combo["reuse"])
    body += f"--{boundary}--\r\n"
    req = urllib.request.Request(
        BASE + "/api/generate",
        data=body.encode(),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["job_id"]


def wait_job(job_id: str, poll_sec: float = 2.0, timeout_sec: float = 900) -> dict:
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            st = api_get(f"/api/jobs/{job_id}")
        except Exception as e:
            return {"status": "poll_error", "error": str(e)}
        if st.get("status") in ("done", "error", "cancelled"):
            return st
        time.sleep(poll_sec)
    return {"status": "timeout"}


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 ram_tier_test.py <target_total_gb>")
    try:
        target_gb = float(sys.argv[1])
    except ValueError:
        sys.exit("target_total_gb must be a number, e.g. 24")

    total = total_ram_gb()
    hold_gb = total - target_gb
    if hold_gb <= 0:
        sys.exit(f"Target {target_gb:.0f}GB >= actual total {total:.1f}GB - nothing to emulate.")
    if target_gb < MIN_FREE_GB:
        sys.exit(f"Refusing: target {target_gb:.0f}GB is below the {MIN_FREE_GB:.0f}GB safety floor.")

    try:
        api_get("/api/jobs")
    except Exception:
        sys.exit(f"Can't reach {BASE} - is the Lite server running (run.sh / launcher)?")

    print(f"Real total RAM: {total:.1f}GB. Emulating {target_gb:.0f}GB total by wiring {hold_gb:.1f}GB.")
    print(f"Free before ballast: {free_ram_gb():.1f}GB")

    size = int(hold_gb * 1024 ** 3)
    buf = mmap.mmap(-1, size, mmap.MAP_PRIVATE | mmap.MAP_ANON)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    arr_type = ctypes.c_char * size
    carr = arr_type.from_buffer(buf)
    addr = ctypes.addressof(carr)

    print("Wiring memory (mlock)...", flush=True)
    ret = libc.mlock(ctypes.c_void_p(addr), ctypes.c_size_t(size))
    if ret != 0:
        errno = ctypes.get_errno()
        print(
            f"WARNING: mlock returned {ret} (errno {errno}) - the ballast may not be "
            f"fully wired. Check Activity Monitor's Free number below against the "
            f"expected ~{target_gb:.0f}GB; if it's way off, this run isn't meaningful."
        )
    time.sleep(1.0)
    actual_free = free_ram_gb()
    print(f"Free after ballast: {actual_free:.1f}GB (target ~{target_gb:.0f}GB)")

    results = []
    try:
        for i, combo in enumerate(COMBOS, 1):
            print(f"[{i}/{len(COMBOS)}] {combo['label']} ...", flush=True)
            free_before = free_ram_gb()
            t0 = time.time()
            try:
                job_id = api_post_generate(combo)
            except Exception as e:
                entry = {**combo, "status": "post_failed", "error": str(e)}
                results.append(entry)
                print(f"  POST FAILED: {e}")
                continue
            result = wait_job(job_id)
            dt = time.time() - t0
            entry = {
                **combo,
                "job_id": job_id,
                "duration_sec": round(dt, 1),
                "status": result.get("status"),
                "peak_rss_mb": result.get("peak_rss_mb"),
                "pageouts_delta": result.get("pageouts_delta"),
                "free_ram_gb_before": round(free_before, 1),
            }
            if result.get("status") == "error":
                entry["log_tail"] = (result.get("log_tail") or "")[-800:]
            results.append(entry)
            print(f"  -> {entry['status']} peak_rss_mb={entry['peak_rss_mb']} ({dt:.0f}s)", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted - releasing ballast and writing partial results...")
    finally:
        print("Releasing ballast...")
        libc.munlock(ctypes.c_void_p(addr), ctypes.c_size_t(size))
        del carr
        buf.close()
        print(f"Free after release: {free_ram_gb():.1f}GB")

    out_path = OUT_DIR / f"ram_tier_results_{target_gb:.0f}gb.json"
    out_path.write_text(json.dumps({
        "target_total_gb": target_gb,
        "real_total_gb": total,
        "actual_free_after_ballast_gb": round(actual_free, 1),
        "results": results,
    }, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
