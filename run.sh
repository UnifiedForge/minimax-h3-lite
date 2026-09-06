#!/bin/bash
# Запуск MiniMax H3 GUI — Lite
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/server"

PORT=8766
URL="http://127.0.0.1:$PORT"

# локальный оверрайд путей (напр. H3_ENGINE_DIR на общий engine/ у разработчика) —
# файл .h3env НЕ в гите, у чистого git clone его просто не будет
[ -f "$DIR/.h3env" ] && source "$DIR/.h3env"

if curl -s -o /dev/null "$URL"; then
  open "$URL"
  exit 0
fi

# первый запуск на новой машине — движка/весов ещё нет: клонируем h3.c,
# собираем, спрашиваем HF-токен и качаем веса. На уже настроенной машине
# (как сейчас у тебя) это no-op — bootstrap_needed вернёт false.
source "$DIR/_bootstrap_engine.sh"

# Если реально нужна настройка (клон/сборка/скачивание — минуты или часы) —
# открываем видимый Terminal и продолжаем уже в нём: вбивать в него ничего не
# нужно (весь ввод — через диалоговые окна поверх), но человеку видно, что
# процесс идёт, а не тишина без понятия — зависло или работает. Если всё уже
# готово — работаем тихо в фоне, как обычно.
if [ -z "${H3_IN_TERMINAL:-}" ] && bootstrap_needed; then
  export H3_IN_TERMINAL=1
  osascript -e "tell application \"Terminal\" to do script \"H3_IN_TERMINAL=1 '$DIR/run.sh'\"" >/dev/null 2>&1
  exit 0
fi

bootstrap_engine

if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate
# venv может протухнуть, если Homebrew обновил/переустановил сам python3 —
# тогда symlink'и внутри venv/bin ведут в никуда
if ! python3 -c "" >/dev/null 2>&1; then
  echo "venv is broken (stale python3 symlinks) — recreating..."
  type deactivate >/dev/null 2>&1 && deactivate
  rm -rf venv
  python3 -m venv venv
  source venv/bin/activate
fi
python3 -m pip --version >/dev/null 2>&1 || python3 -m ensurepip --upgrade
python3 -m pip install -q -r requirements.txt --break-system-packages

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
