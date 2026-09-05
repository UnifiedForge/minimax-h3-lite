#!/bin/bash
# Запуск MiniMax H3 GUI — Lite
set -e
cd "$(dirname "$0")/server"

PORT=8766
URL="http://127.0.0.1:$PORT"

# локальный оверрайд путей (напр. H3_ENGINE_DIR на общий engine/ у разработчика) —
# файл .h3env НЕ в гите, у чистого git clone его просто не будет
[ -f "$(dirname "$0")/.h3env" ] && source "$(dirname "$0")/.h3env"

# если сервер уже поднят (повторный клик по ярлыку, или уже запущен из
# терминала в другом окне) — просто открываем браузер, без повторного
# venv/pip/uvicorn (это ещё и делает двойной клик по .app быстрым)
if curl -s -o /dev/null "$URL"; then
  open "$URL"
  exit 0
fi

# первый запуск на новой машине (свежий git clone Lite с GitHub, например) —
# движка/весов ещё нет: клонируем h3.c, собираем, спрашиваем HF-токен и
# качаем веса. На уже настроенной машине (как сейчас у тебя) это no-op —
# все проверки внутри сразу видят, что всё на месте, и ничего не трогают.
source "$(dirname "$0")/_bootstrap_engine.sh"
bootstrap_engine

if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate
# venv может протухнуть, если Homebrew обновил/переустановил сам python3
# ПОСЛЕ создания venv — symlink'и внутри venv/bin ведут в никуда, и любая
# команда python внутри venv валится как "command not found" (это была
# причина ошибки на первом запуске: venv был создан 1 сентября, python@3.14
# с тех пор явно переустанавливался). Раз в такое напоролись — пересоздаём
# сами, не заставляя тебя чистить venv руками при каждом brew upgrade.
if ! python3 -c "" >/dev/null 2>&1; then
  echo "venv битый (протухшие symlink'и на python3) — пересоздаю..."
  type deactivate >/dev/null 2>&1 && deactivate
  rm -rf venv
  python3 -m venv venv
  source venv/bin/activate
fi
# на некоторых системах venv создаётся без пропатченного бинарника pip
# (или он не первый в PATH) — используем python -m pip, это надёжнее,
# и на всякий случай сначала бутстрапим pip, если модуля вообще нет.
python3 -m pip --version >/dev/null 2>&1 || python3 -m ensurepip --upgrade
python3 -m pip install -q -r requirements.txt --break-system-packages

# стартуем сервер в фоне, ждём пока порт откроется, потом сами открываем
# дефолтный браузер — раньше приходилось руками открывать URL после старта.
# Ctrl+C в этом терминале по-прежнему останавливает всё: фоновый процесс
# висит в той же группе процессов терминала, trap ниже подчищает на всякий.
uvicorn app:app --host 127.0.0.1 --port $PORT &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null" EXIT

for i in $(seq 1 60); do
  if curl -s -o /dev/null "$URL"; then
    open "$URL"
    break
  fi
  sleep 0.5
done

wait $SERVER_PID
