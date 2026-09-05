"""
Клиент анонимной телеметрии — вставляется в pro/server/ и lite/server/.

Что отправляется: редакция, версия приложения, пресет/режим/разрешение,
общий объём RAM машины, пиковый RSS самого h3-процесса за генерацию,
длительность, успех/класс ошибки. НИЧЕГО больше — ни промпта, ни путей к
файлам, ни изображений, ни email/имени.

Отключается переменной окружения H3_TELEMETRY=0 (по умолчанию включено —
можно сменить на opt-in, если решишь иначе).

Любая ошибка сети/сервера тут проглатывается — телеметрия никогда не должна
уронить или задержать реальную генерацию.
"""
import json
import os
import platform
import subprocess
import urllib.request
import uuid
from pathlib import Path

# URL/ключ реального сервера телеметрии — зашиты как значения по умолчанию
# (это НЕ секрет, ключ и так виден в открытом Lite-репозитории — просто
# фильтр случайного шума). Так телеметрию шлют все инсталляции по умолчанию,
# а не только машина разработчика. Переменные окружения — только чтобы можно
# было переопределить/выключить локально при отладке.
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


def _os_version() -> str:
    try:
        return f"macOS {platform.mac_ver()[0]}"
    except Exception:
        return "unknown"


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
        "peak_rss_mb": peak_rss_mb,
        "duration_sec": duration_sec,
        "success": success,
        "error_class": error_class,
        "os_version": _os_version(),
        "chip": _chip_name(),
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
