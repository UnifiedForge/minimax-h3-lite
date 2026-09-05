#!/usr/bin/env python3
"""
Прогон серии генераций через уже работающий Lite-сервер (лучше через Lite —
у Pro смысл тот же движок) для сбора реальной телеметрии по пиковому RAM в
разрезе layers/resolution/steps/reuse/ref-images/длительности.

Ничего не измеряет сам — просто дёргает /api/generate штатным образом и ждёт
завершения. Каждая генерация уже сама шлёт телеметрию (peak RSS, пресет,
разрешение, длительность) на сервер из telemetry_client.py — смотреть
результат потом через report.py на сервере телеметрии, не здесь.

Требования перед запуском:
  1. Lite-сервер должен уже быть запущен (лаунчер или run.sh) — этот скрипт
     только стучится в localhost:8766, сам сервер не поднимает.
  2. В server/telemetry_client.py должны быть прописаны реальные
     TELEMETRY_URL/KEY (иначе события просто никуда не улетят — сама
     генерация всё равно отработает, но данных не будет).

Запуск:  python3 bench_ram.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = "http://127.0.0.1:8766"
REF_IMAGE = Path(__file__).parent / "_bench_ref.png"

RESOLUTIONS = [(512, 512), (768, 768), (768, 1344)]
LAYERS = [40, 45, 50]

COMBOS = []

# 1) основной сет: image-mode (22 кадра, дёшево) — сканируем layers x resolution,
#    steps/reuse держим посередине (баланс между draft и quality)
for w, h in RESOLUTIONS:
    for layers in LAYERS:
        COMBOS.append(dict(
            label=f"img_{w}x{h}_L{layers}",
            width=w, height=h, mode="image",
            preset="custom", steps=20, layers=layers, reuse=2,
        ))

# 2) на самом тяжёлом разрешении/layers смотрим, влияют ли steps/reuse
w, h = RESOLUTIONS[-1]
for steps, reuse in [(4, 1), (50, 1), (20, 3)]:
    COMBOS.append(dict(
        label=f"img_{w}x{h}_L50_s{steps}_r{reuse}",
        width=w, height=h, mode="image",
        preset="custom", steps=steps, layers=50, reuse=reuse,
    ))

# 3) реф-картинки (0 vs 3) на одной базовой точке — содержимое картинки для
#    RAM-теста не важно, важно что код ref-image пути реально отрабатывает
COMBOS.append(dict(
    label="img_768x768_balanced_ref0",
    width=768, height=768, mode="image", preset="balanced", ref_images=0,
))
COMBOS.append(dict(
    label="img_768x768_balanced_ref3",
    width=768, height=768, mode="image", preset="balanced", ref_images=3,
))

# 4) подтверждающие полноразмерные видео-прогоны — по одному на пресет,
#    короткая длительность, чтобы не растягивать на часы
for preset in ["draft", "balanced", "quality"]:
    COMBOS.append(dict(
        label=f"video_5s_{preset}",
        width=768, height=768, mode="video", preset=preset, seconds=5,
    ))


def curl_json(args: list[str]) -> dict:
    out = subprocess.run(["curl", "-s"] + args, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_raw": out.stdout, "_stderr": out.stderr}


def ensure_ref_image() -> Path | None:
    if REF_IMAGE.exists():
        return REF_IMAGE
    try:
        from PIL import Image
        Image.new("RGB", (512, 512), (120, 140, 200)).save(REF_IMAGE)
        return REF_IMAGE
    except ImportError:
        print("Pillow не установлен (pip install pillow) — комбо с ref_images будут пропущены")
        return None


def post_generate(combo: dict, ref_image: Path | None) -> str:
    form = [
        "-F", f"prompt=bench test {combo['label']}",
        "-F", f"width={combo['width']}",
        "-F", f"height={combo['height']}",
        "-F", f"mode={combo['mode']}",
        "-F", f"preset={combo['preset']}",
    ]
    if combo["preset"] == "custom":
        form += [
            "-F", f"steps={combo['steps']}",
            "-F", f"layers={combo['layers']}",
            "-F", f"reuse={combo['reuse']}",
        ]
    if combo["mode"] == "video" and combo.get("seconds"):
        form += ["-F", f"seconds={combo['seconds']}"]
    for _ in range(combo.get("ref_images", 0)):
        form += ["-F", f"ref_images=@{ref_image}"]

    resp = curl_json(form + [f"{BASE}/api/generate"])
    if "job_id" not in resp:
        raise RuntimeError(f"generate failed: {resp}")
    return resp["job_id"]


def wait_job(job_id: str, poll_sec: float = 3.0, timeout_sec: float = 1800) -> dict:
    start = time.time()
    while time.time() - start < timeout_sec:
        st = curl_json([f"{BASE}/api/jobs/{job_id}"])
        if st.get("status") in ("done", "error", "cancelled"):
            return st
        time.sleep(poll_sec)
    return {"status": "timeout"}


def main():
    health = curl_json([f"{BASE}/api/jobs"])
    if "_raw" in health and not health["_raw"]:
        print(f"Не достучался до {BASE} — сервер Lite запущен? (run.sh / ярлык)")
        sys.exit(1)

    ref_image = ensure_ref_image()
    total = len(COMBOS)
    for i, combo in enumerate(COMBOS, 1):
        if combo.get("ref_images") and not ref_image:
            print(f"[{i}/{total}] SKIP {combo['label']} (нет тестовой картинки)")
            continue
        print(f"[{i}/{total}] {combo['label']} ...", flush=True)
        t0 = time.time()
        try:
            job_id = post_generate(combo, ref_image)
        except Exception as e:
            print(f"  ОШИБКА старта: {e}")
            continue
        result = wait_job(job_id)
        dt = time.time() - t0
        print(f"  -> status={result.get('status')} job={job_id} ({dt:.0f}s)")


if __name__ == "__main__":
    main()
