"""
Минимальная морда поверх h3.c (MiniMax H3 на Apple Silicon / Metal).

Запуск (на самом Маке, не в облаке — h3 это нативный Mach-O бинарник):

    cd ~/MiniMaxH3/server
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn app:app --host 127.0.0.1 --port 8765

Потом открыть http://127.0.0.1:8765 в браузере.

Предполагает, что:
  - ~/MiniMaxH3/engine/h3c-src/h3       — собранный бинарник (make -j8)
  - ~/MiniMaxH3/engine/MiniMax-H3       — скачанные веса (FL2VA/ + Ref2VA/transformer/)
Оба пути настраиваются через переменные окружения H3_BIN / H3_MODEL_DIR при желании.
"""
import asyncio
import base64
import re
import json
import logging
import os
import signal
import shlex
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# морда опрашивает /api/jobs каждые 2 сек — эти запросы засоряют терминал,
# глушим access-log независимо от того, каким флагом запущен uvicorn.
logging.getLogger("uvicorn.access").disabled = True
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from telemetry_client import send_generation_event, sample_pre_generation

EDITION = "lite"  # используется только в телеметрии

# --- пути -------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# По умолчанию движок (h3-бинарник + веса, ~196GB) лежит СВОИМ внутри этой же
# копии (./engine) — это обязательно для того, кто просто склонировал репозиторий
# с GitHub, у него никакого соседнего engine/ не будет. H3_ENGINE_DIR — локальный
# оверрайд для машины разработчика (см. .h3env, не в гите), где Pro и Lite стоят
# рядом и шарят один engine/, чтобы не хранить/качать 196GB дважды.
ENGINE_DIR = Path(os.environ.get("H3_ENGINE_DIR", PROJECT_ROOT / "engine"))

H3_BIN = Path(os.environ.get("H3_BIN", ENGINE_DIR / "h3c-src" / "h3"))
H3_MODEL_DIR = Path(os.environ.get("H3_MODEL_DIR", ENGINE_DIR / "MiniMax-H3"))

JOBS_DIR = PROJECT_ROOT / "server" / "jobs"      # логи + загруженные референсы, по одному на job
OUTPUTS_DIR = PROJECT_ROOT / "outputs"           # готовые mp4
WEB_DIR = PROJECT_ROOT / "web"

for d in (JOBS_DIR, OUTPUTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- валидированные пресеты из README h3.c -----------------------------
# (steps, layers, reuse) — см. таблицу "Choose a speed/quality preset"
PRESETS = {
    "draft":    {"steps": 4,  "layers": 50, "reuse": 1},   # ~3.5s/22 кадра на M5 Max
    "balanced": {"steps": 20, "layers": 45, "reuse": 2},   # рекомендованная стартовая точка
    "quality":  {"steps": 50, "layers": 50, "reuse": 1},   # close-reference оракул, дорого
}

# допустимые канвасы (кратны 32, произведение <= 768*1344) — просто ограничиваем сверху,
# сама модель отвергнет некорректную комбинацию с понятной ошибкой в логе.
MAX_PIXELS = 768 * 1344

# --- image-mode (одна картинка вместо видео) -------------------------------
# У h3.c нет нативного режима "просто одна картинка" — минимальная единица
# генерации это один декодерный чанк видео-VAE, который h3_align_frame_count()
# (h3_host.c) всегда выравнивает вверх минимум до 22 кадров: сам h3.c прямо
# отказывается генерить меньше ("generation requires at least one trained
# 22-frame decoder chunk", h3.c:861-864). Ровно так же устроен TI2I в исходном
# ComfyUI-воркфлоу — там просится короткий мини-клип ("recommended | 5 frames"
# на уровне латента), из него декодируется пачка кадров, и один лучший кадр
# автоматически выбирает нода H3ImageFrameSelector (mode='best_quality').
# Тут тот же трюк: просим у h3 минимально возможный клип (22 кадра, ~0.9 сек
# на 24fps — это быстрее любого настоящего видео), а лучший кадр из результата
# выбираем сами — по резкости (variance of Laplacian), см. _extract_best_frame.
IMAGE_MODE_FRAMES = 22

# --- локальный промпт-энхенсер через LM Studio (OpenAI-совместимый API) ----
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma-4-31b-it-heretic-i1_nd")

PROMPTS_DIR = PROJECT_ROOT / "reference" / "prompts"
ENHANCER_PRESETS = {
    "naxdy": "MiniMax H3 by Naxdy.txt",
    "character_sheet": "Character Sheet 3x2 (MMH3).txt",
    "ti2i_storyboard": "MiniMax H3 - TI2I - Storyboard by Pavel.txt",
}

app = FastAPI(title="MiniMax H3 (h3.c) — minimal wrapper")

# --- job state (в памяти — этого достаточно для локального однопользовательского инструмента) --

jobs: dict[str, dict] = {}
running_procs: dict[str, asyncio.subprocess.Process] = {}  # job_id -> живой процесс, для cancel/cleanup
# FIFO-очередь: h3 сам жрёт всю доступную RAM/GPU на генерацию, поэтому запускаем
# строго один процесс за раз. Раньше /api/generate сразу create_task'ил _run_h3 —
# случайный второй клик стартовал вторую генерацию параллельно с первой, деля
# память/GPU пополам и вдвое замедляя обе.
job_queue: asyncio.Queue[str] = asyncio.Queue()


def _kill_all_running():
    """Убивает все ещё живые дочерние h3-процессы. h3 сам держит GPU занятым,
    пока жив, поэтому просто остановить uvicorn (Ctrl+C) недостаточно —
    процесс остаётся сиротой и молотит видеокарту дальше."""
    for job_id, proc in list(running_procs.items()):
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


async def _queue_worker():
    """Единственный воркер разбирает очередь строго последовательно."""
    while True:
        job_id = await job_queue.get()
        try:
            job = jobs.get(job_id)
            if job and job["status"] == "queued":
                await _run_h3(job_id, job["argv"])
            # иначе задачу отменили, пока она стояла в очереди (см. _terminate_job) — пропускаем
        finally:
            job_queue.task_done()


@app.on_event("startup")
async def _on_startup():
    asyncio.create_task(_queue_worker())


@app.on_event("shutdown")
async def _on_shutdown():
    _kill_all_running()
    # даём секунду на мягкое завершение, потом добиваем
    await asyncio.sleep(1)
    for proc in running_procs.values():
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass



def _render_terminal(raw: bytes) -> str:
    """h3 печатает прогресс в стиле терминала: пишет '\r<фаза>   N/M   '
    поверх предыдущей такой же строки, и настоящий '\n' — только когда
    фаза целиком завершилась и начинается следующая. При редиректе stdout
    в файл (как у нас) эти управляющие символы попадают в файл как есть —
    их нельзя просто .split('\\n'), нужно честно эмулировать курсор,
    как это делает сам терминал."""
    text = raw.decode(errors="replace")
    lines = [""]
    row, col = 0, 0
    for ch in text:
        if ch == "\n":
            row += 1
            if row == len(lines):
                lines.append("")
            col = 0
        elif ch == "\r":
            col = 0
        else:
            line = lines[row]
            if col < len(line):
                lines[row] = line[:col] + ch + line[col + 1:]
            else:
                lines[row] = line + " " * (col - len(line)) + ch
            col += 1
    return "\n".join(l.rstrip() for l in lines)


def _job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


_FOOTPRINT_UNIT_TO_KB = {"K": 1, "M": 1024, "G": 1024 * 1024, "T": 1024 * 1024 * 1024}
_FOOTPRINT_PEAK_RE = re.compile(r"Physical footprint \(peak\):\s+([\d.]+)([KMGT])")


async def _poll_peak_rss_kb(pid: int, stop: asyncio.Event) -> int:
    """Каждые ~1.5с смотрит "Physical footprint (peak)" процесса h3 через
    vmmap --summary — то же число, что показывает Activity Monitor и что
    реально использует macOS для решений об OOM (jetsam). Раньше тут был
    ps -o rss=, но на реальном прогоне (768x768/quality video) он занизил
    пик на ~7ГБ против Activity Monitor (13.6 vs 20.98) — похоже, ps rss не
    полностью учитывает Metal/GPU unified-memory footprint движка на Apple
    Silicon, а vmmap footprint учитывает. vmmap сам трекает пик за всё время
    жизни процесса на уровне ядра, поэтому просто берём "(peak)" из каждого
    замера — max() тут просто подстраховка на случай сбоя парсинга разового
    замера, а не замена кумулятивного трекинга ядра.

    Название функции/поле телеметрии (peak_rss_mb) оставлены как есть, чтобы
    не гонять ещё одну миграцию схемы на сервере телеметрии — по сути это
    теперь Physical Footprint, а не classic RSS."""
    peak_kb = 0
    while not stop.is_set():
        try:
            p = await asyncio.create_subprocess_exec(
                "vmmap", "--summary", str(pid),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await p.communicate()
            m = _FOOTPRINT_PEAK_RE.search(out.decode(errors="ignore"))
            if m:
                val_kb = int(float(m.group(1)) * _FOOTPRINT_UNIT_TO_KB[m.group(2)])
                if val_kb > peak_kb:
                    peak_kb = val_kb
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
    return peak_kb


async def _run_h3(job_id: str, argv: list[str]):
    job = jobs[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()
    log_path = _job_dir(job_id) / "log.txt"
    job["log_path"] = str(log_path)

    mem_before = await asyncio.to_thread(sample_pre_generation)
    with open(log_path, "wb") as logf:
        logf.write((" ".join(shlex.quote(a) for a in argv) + "\n\n").encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(H3_BIN.parent),
            stdout=logf,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # своя группа процессов — можно прибить вместе с ffmpeg-детьми
        )
        job["pid"] = proc.pid
        running_procs[job_id] = proc
        rss_stop = asyncio.Event()
        rss_task = asyncio.create_task(_poll_peak_rss_kb(proc.pid, rss_stop))
        try:
            returncode = await proc.wait()
        finally:
            rss_stop.set()
            peak_rss_kb = await rss_task
            running_procs.pop(job_id, None)
    mem_after = await asyncio.to_thread(sample_pre_generation)
    pageouts_delta = mem_after.get("pageouts", 0) - mem_before.get("pageouts", 0)

    job["finished_at"] = time.time()
    job["returncode"] = returncode
    duration_sec = job["finished_at"] - job["started_at"]
    if job["status"] == "cancelled":
        pass  # уже помечено через /cancel — не перетираем на "error" из-за SIGTERM
    else:
        job["status"] = "done" if returncode == 0 else "error"
        if job["status"] == "done" and job.get("mode") == "image":
            try:
                await asyncio.to_thread(_extract_best_frame, job_id)
                image_path = OUTPUTS_DIR / f"{job_id}.png"
                with open(log_path, "ab") as logf:
                    logf.write(f"h3-wrapper: выбран лучший кадр -> {image_path}\n".encode())
            except Exception as e:
                # не роняем job целиком — mp4-чанк всё равно готов, просто
                # картинку вытащить не смогли (например нет opencv в venv)
                job["image_error"] = str(e)
                with open(log_path, "ab") as logf:
                    logf.write(f"h3-wrapper: не удалось выбрать кадр из клипа: {e}\n".encode())

    if job["status"] in ("done", "error"):
        # анонимная телеметрия (см. telemetry_client.py) — fire-and-forget,
        # никогда не должна задерживать или ронять сам job
        asyncio.create_task(
            asyncio.to_thread(
                send_generation_event,
                edition=EDITION,
                mode=job.get("mode", "video"),
                preset=job.get("preset", ""),
                resolution=job.get("resolution", ""),
                seconds=job.get("seconds"),
                frames=job.get("frames"),
                peak_rss_mb=(peak_rss_kb / 1024) if peak_rss_kb else None,
                duration_sec=duration_sec,
                success=(job["status"] == "done"),
                error_class=None if job["status"] == "done" else f"exit_{returncode}",
                free_ram_gb_start=mem_before.get("free_ram_gb"),
                pageouts_delta=pageouts_delta,
                ref_image_count=job.get("ref_image_count", 0),
                has_frame_anchor=job.get("has_frame_anchor", False),
                layers=job.get("layers"),
                steps=job.get("steps"),
                reuse=job.get("reuse"),
            )
        )


def _terminate_job(job_id: str) -> bool:
    job = jobs.get(job_id)
    proc = running_procs.get(job_id)
    if proc and proc.returncode is None:
        job["status"] = "cancelled"
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return True
    if job and job["status"] == "queued":
        # ещё не стартовала — воркер увидит "cancelled" и молча пропустит эту задачу
        job["status"] = "cancelled"
        return True
    return False


def _extract_best_frame(job_id: str) -> None:
    """image-mode: из готового мини-клипа (IMAGE_MODE_FRAMES кадров) выбираем
    один — по резкости (variance of Laplacian), тот же принцип, что и
    best_quality в оригинальном H3ImageFrameSelector. Не берём крайние кадры
    декодерного чанка (первый/последний чаще смазаны/недоформированы) —
    смотрим только на центральные ~70%."""
    import cv2

    video_path = OUTPUTS_DIR / f"{job_id}.mp4"
    if not video_path.exists():
        raise RuntimeError(f"{video_path} not found")

    cap = cv2.VideoCapture(str(video_path))
    frames = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        cap.release()
    if not frames:
        raise RuntimeError("no frames decoded from output mp4")

    n = len(frames)
    lo = int(n * 0.15)
    hi = max(lo + 1, int(n * 0.85))
    candidates = frames[lo:hi] or frames

    def sharpness(f):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    best = max(candidates, key=sharpness)
    out_path = OUTPUTS_DIR / f"{job_id}.png"
    cv2.imwrite(str(out_path), best)


def _build_argv(job_id: str, params: dict, first_frame: Optional[Path],
                 last_frame: Optional[Path], ref_images: list[Path]) -> list[str]:
    if not H3_BIN.exists():
        raise HTTPException(400, f"h3 binary not found at {H3_BIN} — сначала собери его (make -j8)")
    if not H3_MODEL_DIR.exists():
        raise HTTPException(400, f"model dir not found at {H3_MODEL_DIR} — сначала скачай веса")

    if (first_frame or last_frame) and ref_images:
        raise HTTPException(400, "first/last-frame anchors нельзя сочетать с ref-image референсами")
    # Lite: identity-референс ограничен 3 фото (в Pro — до 9, максимум который тянет h3.c)
    if ref_images and len(ref_images) > 3:
        raise HTTPException(400, "В Lite-версии референс-режим ограничен 3 картинками (Pro — до 9)")

    w, h = int(params["width"]), int(params["height"])
    if w % 32 or h % 32 or w < 32 or h < 32 or w * h > MAX_PIXELS:
        raise HTTPException(400, "width/height должны быть кратны 32 и не превышать 768x1344 по площади")

    preset_name = params.get("preset", "balanced")
    if preset_name == "custom":
        steps = int(params["steps"])
        layers = int(params["layers"])
        reuse = int(params["reuse"])
    else:
        preset = PRESETS.get(preset_name, PRESETS["balanced"])
        steps, layers, reuse = preset["steps"], preset["layers"], preset["reuse"]

    out_path = OUTPUTS_DIR / f"{job_id}.mp4"

    argv = [
        str(H3_BIN),
        "-d", str(H3_MODEL_DIR),
        "-p", params["prompt"],
        "--width", str(w),
        "--height", str(h),
        "--steps", str(steps),
        "--layers", str(layers),
        "--reuse", str(reuse),
        "-o", str(out_path),
    ]

    if params.get("mode") == "image":
        # игнорируем то, что пришло с фронта в seconds/frames — картинке нужен
        # ровно минимальный чанк, любое другое значение либо ничего не изменит
        # (выровняется к тем же 22), либо (при seconds) сгенерит лишнее видео
        argv += ["--frames", str(IMAGE_MODE_FRAMES)]
    else:
        seconds = params.get("seconds")
        frames = params.get("frames")
        if frames:
            argv += ["--frames", str(int(frames))]
        elif seconds:
            argv += ["--seconds", str(float(seconds))]

    seed = params.get("seed")
    if seed not in (None, "", "random"):
        argv += ["--seed", str(int(seed))]

    if params.get("token_reduction"):
        argv.append("--token-reduction")

    if first_frame:
        argv += ["--first-frame", str(first_frame)]
    if last_frame:
        argv += ["--last-frame", str(last_frame)]
    for p in ref_images:
        argv += ["--ref-image", str(p)]

    return argv


def _argv_value(argv: list[str], flag: str) -> Optional[str]:
    """Достаёт значение опции вида --flag VALUE из уже собранного argv —
    надёжнее, чем дублировать логику выбора preset/custom для телеметрии."""
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _load_enhancer_system_prompt(preset: str) -> str:
    fname = ENHANCER_PRESETS.get(preset)
    if not fname:
        raise HTTPException(400, f"неизвестный пресет энхенсера: {preset}")
    path = PROMPTS_DIR / fname
    if not path.exists():
        raise HTTPException(400, f"файл системного промпта не найден: {path}")
    return path.read_text(encoding="utf-8")


def _call_llm(system_prompt: str, user_text: str, image_b64: Optional[str] = None) -> str:
    """Синхронный блокирующий вызов LM Studio — гоняется через
    asyncio.to_thread, чтобы не морозить event loop сервера на время
    ответа модели (может занимать десятки секунд на 31B)."""
    content: list = []
    if image_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
    content.append({"type": "text", "text": user_text})

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.8,
        # не режем нормальную длину ответа (у Naxdy-промпта она сама по себе
        # немаленькая, за 1000+ токенов) — это просто страховка от зацикливания
        # модели (частый failure mode локальных LLM), а не лимит на контент.
        "max_tokens": 3000,
    }
    req = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise HTTPException(502, f"не достучался до LLM-сервера ({LLM_BASE_URL}): {e}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise HTTPException(502, f"неожиданный ответ LLM-сервера: {data}")


# Промпт-энхенсер — Pro-эксклюзив (решено 2026-09-05). Эндпоинты оставлены,
# но отдают 403 — на случай если кто-то раскомментит кнопку на фронте руками.
@app.get("/api/enhance/presets")
async def enhance_presets():
    raise HTTPException(403, "Промпт-энхенсер доступен только в Pro-версии")


@app.post("/api/enhance")
async def enhance(
    prompt: str = Form(...),
    preset: str = Form("naxdy"),
    image: Optional[UploadFile] = File(None),
):
    raise HTTPException(403, "Промпт-энхенсер доступен только в Pro-версии")


@app.post("/api/generate")
async def generate(
    prompt: str = Form(...),
    width: int = Form(512),
    height: int = Form(512),
    seconds: Optional[float] = Form(None),
    frames: Optional[int] = Form(None),
    preset: str = Form("balanced"),
    steps: Optional[int] = Form(None),
    layers: Optional[int] = Form(None),
    reuse: Optional[int] = Form(None),
    seed: Optional[str] = Form(None),
    token_reduction: bool = Form(False),
    mode: str = Form("video"),  # "video" | "image" — см. IMAGE_MODE_FRAMES выше
    first_frame: Optional[UploadFile] = File(None),
    last_frame: Optional[UploadFile] = File(None),
    ref_images: list[UploadFile] = File(default=[]),
):
    job_id = uuid.uuid4().hex[:12]
    jdir = _job_dir(job_id)

    async def save(upload: UploadFile, name: str) -> Path:
        dest = jdir / name
        data = await upload.read()
        dest.write_bytes(data)
        return dest

    first_frame_path = await save(first_frame, "first_frame" + Path(first_frame.filename).suffix) if first_frame and first_frame.filename else None
    last_frame_path = await save(last_frame, "last_frame" + Path(last_frame.filename).suffix) if last_frame and last_frame.filename else None
    ref_paths = []
    for i, f in enumerate([f for f in ref_images if f and f.filename]):
        ref_paths.append(await save(f, f"ref_{i}{Path(f.filename).suffix}"))

    params = dict(prompt=prompt, width=width, height=height, seconds=seconds,
                  frames=frames, preset=preset, steps=steps, layers=layers,
                  reuse=reuse, seed=seed, token_reduction=token_reduction, mode=mode)
    argv = _build_argv(job_id, params, first_frame_path, last_frame_path, ref_paths)
    # реальные steps/layers/reuse берём из уже собранного argv, а не из params —
    # там при preset != "custom" они всё ещё None (сырые form-данные с фронта)
    argv_layers = _argv_value(argv, "--layers")
    argv_steps = _argv_value(argv, "--steps")
    argv_reuse = _argv_value(argv, "--reuse")

    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "created_at": time.time(),
        "prompt": prompt,
        "argv": argv,
        "mode": mode,
        "preset": preset,
        "resolution": f"{width}x{height}",
        "seconds": seconds,
        "frames": frames,
        "layers": int(argv_layers) if argv_layers else None,
        "steps": int(argv_steps) if argv_steps else None,
        "reuse": int(argv_reuse) if argv_reuse else None,
        "ref_image_count": len(ref_paths),
        "has_frame_anchor": bool(first_frame_path or last_frame_path),
    }
    await job_queue.put(job_id)
    return {"job_id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    return sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    log_tail = ""
    log_path = job.get("log_path")
    if log_path and os.path.exists(log_path):
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # с запасом: терминальная эмуляция схлопывает \r-обновления в
            # одну строку на фазу, так что итоговый текст всегда короткий,
            # даже если сырых байт (из-за частых \r) в хвосте было много.
            f.seek(max(0, size - 400_000))
            log_tail = _render_terminal(f.read())
    out = {**job, "log_tail": log_tail}
    if job.get("status") == "queued":
        queued_ids = sorted(
            (j["id"] for j in jobs.values() if j["status"] == "queued"),
            key=lambda jid: jobs[jid]["created_at"],
        )
        out["queue_position"] = queued_ids.index(job_id) + 1
    # video_ready раньше проверял только exists() — а FFmpeg (внутри h3)
    # создаёт/усекает выходной файл ещё в начале своей фазы кодирования,
    # задолго до того, как весь процесс реально закончит писать в него.
    # На длинных генерациях (где сама FFmpeg-фаза занимает время) клиент
    # успевал схватить src на недописанный файл и больше никогда не
    # перечитывал — отсюда "чёрное видео 0:00" при на самом деле готовом
    # результате. Ждём полного выхода процесса (status == done).
    out["video_ready"] = (
        job.get("status") == "done"
        and (OUTPUTS_DIR / f"{job_id}.mp4").exists()
    )
    out["image_ready"] = (
        job.get("status") == "done"
        and job.get("mode") == "image"
        and (OUTPUTS_DIR / f"{job_id}.png").exists()
    )
    return JSONResponse(out)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    ok = _terminate_job(job_id)
    if not ok:
        raise HTTPException(409, "job is not running")
    return {"cancelled": True}


@app.get("/api/jobs/{job_id}/video")
async def job_video(job_id: str):
    path = OUTPUTS_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "video not ready")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/image")
async def job_image(job_id: str):
    path = OUTPUTS_DIR / f"{job_id}.png"
    if not path.exists():
        raise HTTPException(404, "image not ready")
    return FileResponse(path, media_type="image/png")


@app.get("/")
async def index():
    return HTMLResponse((WEB_DIR / "index.html").read_text())

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
