#!/usr/bin/env python3
"""
Emulates a lower-RAM Mac on THIS SAME machine by wiring (mlock) enough
memory to bring *free* RAM down to roughly TARGET_FREE_GB, then fires a
battery of real generations at the already-running Lite server and records
what happens (success / error / timeout) plus duration and (when available)
peak memory footprint. Output is a JSON file you hand back for building the
RAM-tier table.

v2: the ballast size used to be computed from nominal TOTAL RAM
(hw.memsize), which silently assumed 0 baseline OS/background usage. On
this machine baseline usage is ~35GB (128GB total, ~93GB actually free
with normal apps open), so that math was badly wrong in both directions:
a "16GB" run only wired ~0.2GB (asked to wire 112GB, way past what was
actually free, so mlock() failed almost immediately and the run silently
proceeded at basically full/unthrottled speed) while a "24GB" run wired
far MORE than intended (ended up around ~11GB free, not ~24GB). Neither
run's timings meant what their filename claimed.

Fix: the ballast is now sized off *measured* free RAM at run time
(hold_gb = current_free - target_free_gb), wired in chunks (so a partial
failure is visible and bounded instead of all-or-nothing), and the actual
result is verified against the target after wiring - if it's off by more
than a small tolerance, the script aborts BEFORE running any generations
instead of burning 10-60+ minutes on data that doesn't represent what its
filename says.

This still isn't a perfect stand-in for a real smaller Mac (unified-memory
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
     not skewed by your own open apps - and so the target is actually
     reachable (you can't emulate 48GB free if only 40GB is free to begin
     with; the script now checks this up front and tells you plainly).
  3. Keep Activity Monitor open while this runs. If the WHOLE Mac starts
     feeling unresponsive (not just h3 - Finder, menu bar, mouse cursor),
     Ctrl+C immediately. The MIN_FREE_GB floor below is a safety net, not
     a guarantee.

USAGE:
    python3 ram_tier_test.py 24      # target ~24GB free after ballast
    python3 ram_tier_test.py 16      # target ~16GB free after ballast
    python3 ram_tier_test.py 32
    python3 ram_tier_test.py 48

("target free", not "target total" - see the v2 note above for why.)

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
MIN_FREE_GB = 6.0  # never target below this - leaving less than this free
                    # risks the OS itself (WindowServer, mouse/keyboard) hanging.
CHUNK_GB = 4.0      # wire in chunks so a partial failure is visible/bounded
                    # instead of one giant mlock() either fully succeeding or
                    # (as observed) failing almost completely with ~0 wired.
TOLERANCE_GB = 3.0  # how far actual free-after-ballast may drift from the
                    # target before the run is considered not meaningful
                    # and aborted before any generations run.
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


def wire_ballast(size: int):
    """mlock()s `size` bytes in CHUNK_GB pieces, stopping (not raising) at
    the first chunk that fails. Returns (buf, carr, addr, wired_bytes) -
    wired_bytes may be less than size if a chunk failed."""
    buf = mmap.mmap(-1, size, mmap.MAP_PRIVATE | mmap.MAP_ANON)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    arr_type = ctypes.c_char * size
    carr = arr_type.from_buffer(buf)
    addr = ctypes.addressof(carr)

    chunk_bytes = int(CHUNK_GB * 1024 ** 3)
    wired = 0
    while wired < size:
        this_chunk = min(chunk_bytes, size - wired)
        ret = libc.mlock(ctypes.c_void_p(addr + wired), ctypes.c_size_t(this_chunk))
        if ret != 0:
            errno = ctypes.get_errno()
            print(
                f"  stopped wiring at {wired / 1024**3:.1f}GB / "
                f"{size / 1024**3:.1f}GB requested (mlock errno {errno} on next "
                f"{CHUNK_GB:.0f}GB chunk) - hit a system limit, not a crash."
            )
            break
        wired += this_chunk
        print(f"  wired {wired / 1024**3:.1f}GB / {size / 1024**3:.1f}GB...", end="\r", flush=True)
    print()
    return libc, buf, carr, addr, wired


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 ram_tier_test.py <target_free_gb>")
    try:
        target_gb = float(sys.argv[1])
    except ValueError:
        sys.exit("target_free_gb must be a number, e.g. 24")
    if target_gb < MIN_FREE_GB:
        sys.exit(f"Refusing: target {target_gb:.0f}GB is below the {MIN_FREE_GB:.0f}GB safety floor.")

    try:
        api_get("/api/jobs")
    except Exception:
        sys.exit(f"Can't reach {BASE} - is the Lite server running (run.sh / launcher)?")

    total = total_ram_gb()
    free_before = free_ram_gb()
    hold_gb = free_before - target_gb
    if hold_gb <= 0:
        sys.exit(
            f"Target {target_gb:.0f}GB free >= current free {free_before:.1f}GB - "
            f"nothing to hold. Close other apps to free up more headroom, or this "
            f"tier isn't reachable on this machine right now."
        )

    print(f"Real total RAM: {total:.1f}GB. Free right now: {free_before:.1f}GB.")
    print(f"Target: ~{target_gb:.0f}GB free -> wiring {hold_gb:.1f}GB.")

    print("Wiring memory (mlock, in chunks)...", flush=True)
    libc, buf, carr, addr, wired_bytes = wire_ballast(int(hold_gb * 1024 ** 3))
    time.sleep(1.0)
    actual_free = free_ram_gb()
    drift = actual_free - target_gb
    print(f"Free after ballast: {actual_free:.1f}GB (target ~{target_gb:.0f}GB, drift {drift:+.1f}GB)")

    if abs(drift) > TOLERANCE_GB:
        print(
            f"\nABORTING before running any generations: actual free "
            f"({actual_free:.1f}GB) is more than {TOLERANCE_GB:.0f}GB off the "
            f"{target_gb:.0f}GB target, so this run wouldn't represent a "
            f"{target_gb:.0f}GB tier. Releasing ballast now."
        )
        libc.munlock(ctypes.c_void_p(addr), ctypes.c_size_t(len(carr)))
        del carr
        buf.close()
        print(f"Free after release: {free_ram_gb():.1f}GB")
        sys.exit(1)

    results = []
    try:
        for i, combo in enumerate(COMBOS, 1):
            print(f"[{i}/{len(COMBOS)}] {combo['label']} ...", flush=True)
            free_now = free_ram_gb()
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
                "free_ram_gb_before": round(free_now, 1),
            }
            if result.get("status") == "error":
                entry["log_tail"] = (result.get("log_tail") or "")[-800:]
            results.append(entry)
            print(f"  -> {entry['status']} peak_rss_mb={entry['peak_rss_mb']} ({dt:.0f}s)", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted - releasing ballast and writing partial results...")
    finally:
        print("Releasing ballast...")
        libc.munlock(ctypes.c_void_p(addr), ctypes.c_size_t(len(carr)))
        del carr
        buf.close()
        print(f"Free after release: {free_ram_gb():.1f}GB")

    out_path = OUT_DIR / f"ram_tier_results_{target_gb:.0f}gb.json"
    out_path.write_text(json.dumps({
        "target_free_gb": target_gb,
        "real_total_gb": total,
        "free_before_ballast_gb": round(free_before, 1),
        "wired_gb": round(wired_bytes / 1024 ** 3, 1),
        "actual_free_after_ballast_gb": round(actual_free, 1),
        "results": results,
    }, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
