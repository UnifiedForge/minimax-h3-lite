"""
Клиент анонимной телеметрии — вставляется в pro/server/ и lite/server/.

Что отправляется: редакция, версия приложения, пресет/режим/разрешение,
объём и модель железа (hw.model — надёжнее названия чипа), общий объём RAM,
свободная RAM на СТАРТЕ генерации (не только общий объём — важно, был ли
реальный запас), пиковый RSS самого h3-процесса, признак использования
reference-картинок/frame-анкоров, дельту Pageouts за время генерации (сигнал
"было ли давление на память" — отличает "уместилось с запасом" от
"уместилось, но со свопом"), длительность, успех/класс ошибки. НИЧЕГО
больше — ни промпта, ни путей к файлам, ни изображений, ни email/имени.

Отключается переменной окружения H3_TELEMETRY=0 (по умолчанию включено —
можно сменить на opt-in, если решишь иначе).

Любая ошибка сети/сервера тут проглатывается — телеметрия никогда не должна
уронить или задержать реальную генерацию.
"""
import json
import os
import platform
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path

_DEFAULT_TELEMETRY_URL = "https://telemetry.unifiedforge.dev/v1/events"
_DEFAULT_TELEMETRY_KEY = "14093ee186cd2d66bfe96947499d0ac47fe00fdde0706d47"

TELEMETRY_ENABLED = os.environ.get("H3_TELEMETRY", "1") != "0"
TELEMETRY_URL = os.environ.get("H3_TELEMETRY_URL", _DEFAULT_TELEMETRY_URL)
TELEMETRY_KEY = os.environ.get("H3_TELEMETRY_KEY", _DEFAULT_TELEMETRY_KEY)
APP_VERSION = os.environ.get("H3_APP_VERSION", "0.1")

_INSTALL_ID_FILE = Path(__file__).parent / ".install_id"  # gitignored


def _install_id() -> str:
    try:
        if _INSTALL_ID_FILE.exists():
            return _INSTALL_ID_FILE.read_text().strip()
        new_id = uuid.uuid4().hex
        _INSTALL_ID_FILE.write_text(new_id)
        return new_id
    except Exception:
        return "unknown"


def _total_ram_gb() -> float:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2
        )
        return round(int(out.stdout.strip()) / (1024**3), 1)
    except Exception:
        return 0.0


def _chip_name() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        name = out.stdout.strip()
        return name or "unknown"
    except Exception:
        return "unknown"


def _hw_model() -> str:
    # надёжнее chip_name — hw.model (напр. "Mac16,5") заполнен всегда, в
    # отличие от machdep.cpu.brand_string, которое на части конфигураций
    # Apple Silicon пустое или generic
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.model"], capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _os_version() -> str:
    try:
        return f"macOS {platform.mac_ver()[0]}"
    except Exception:
        return "unknown"


_VM_STAT_LINE_RE = re.compile(r"^(.+?):\s+(\d+)\.?\s*$")


def _vm_stat() -> dict:
    """Парсит вывод vm_stat в {имя_строки: число} + page_size (байт)."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        lines = out.stdout.splitlines()
        result: dict = {}
        if lines:
            m = re.search(r"page size of (\d+) bytes", lines[0])
            result["_page_size"] = int(m.group(1)) if m else 4096
        for line in lines[1:]:
            m = _VM_STAT_LINE_RE.match(line.strip())
            if m:
                result[m.group(1)] = int(m.group(2))
        return result
    except Exception:
        return {}


def sample_pre_generation() -> dict:
    """Снимок состояния памяти ДО запуска h3 — вызывать перед стартом
    subprocess'а. free_ram_gb — грубая оценка доступной памяти (free +
    inactive страницы), pageouts — счётчик с момента загрузки системы
    (кумулятивный) для последующего вычисления дельты за время генерации."""
    vm = _vm_stat()
    page = vm.get("_page_size", 4096)
    free_pages = vm.get("Pages free", 0) + vm.get("Pages inactive", 0)
    return {
        "free_ram_gb": round(free_pages * page / (1024**3), 1),
        "pageouts": vm.get("Pageouts", 0),
    }


def send_generation_event(
    *,
    edition: str,
    mode: str,
    preset: str,
    resolution: str = "",
    seconds: float | None = None,
    frames: int | None = None,
    peak_rss_mb: float | None = None,
    duration_sec: float | None = None,
    success: bool,
    error_class: str | None = None,
    free_ram_gb_start: float | None = None,
    pageouts_delta: int | None = None,
    ref_image_count: int = 0,
    has_frame_anchor: bool = False,
) -> None:
    if not TELEMETRY_ENABLED or not TELEMETRY_URL:
        return
    payload = {
        "install_id": _install_id(),
        "edition": edition,
        "app_version": APP_VERSION,
        "event": "generation_done",
        "mode": mode,
        "preset": preset,
        "resolution": resolution,
        "seconds": seconds,
        "frames": frames,
        "total_ram_gb": _total_ram_gb(),
        "free_ram_gb_start": free_ram_gb_start,
        "peak_rss_mb": peak_rss_mb,
        "pageouts_delta": pageouts_delta,
        "ref_image_count": ref_image_count,
        "has_frame_anchor": has_frame_anchor,
        "duration_sec": duration_sec,
        "success": success,
        "error_class": error_class,
        "os_version": _os_version(),
        "chip": _chip_name(),
        "hw_model": _hw_model(),
    }
    try:
        req = urllib.request.Request(
            TELEMETRY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-H3-Key": TELEMETRY_KEY},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # телеметрия не должна ронять генерацию
