#!/bin/bash
# Общий бутстрап движка (engine/) — источник в pro/run.sh и lite/run.sh,
# т.к. движок (h3.c бинарник + веса модели) общий на обе редакции.
#
# Ничего не делает, если всё уже на месте (как сейчас на этой машине) —
# это специально для НОВОГО пользователя, который склонировал Lite с GitHub:
# у него нет ни исходников h3.c, ни собранного бинарника, ни весов модели,
# и без терминала (двойной клик по .app) их взять неоткуда, кроме как отсюда.
#
# Использование: source этот файл, потом (по желанию) проверить
# bootstrap_needed, затем вызвать bootstrap_engine.

# По умолчанию движок качается прямо сюда же (./engine, рядом с этим файлом) —
# так работает для чистого git clone. H3_ENGINE_DIR — локальный оверрайд
# (см. .h3env, не в гите) для машины разработчика, где engine/ общий на
# несколько копий проекта.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="${H3_ENGINE_DIR:-$REPO_ROOT/engine}"
H3_SRC="$ENGINE_DIR/h3c-src"
H3_BIN="$H3_SRC/h3"
MODEL_DIR="$ENGINE_DIR/MiniMax-H3"

_dialog() {
  # простой информационный диалог с одной кнопкой OK — блокирует до клика,
  # так что шаги идут строго по порядку и юзер видит что происходит
  osascript -e "display dialog \"$1\" with title \"MiniMax H3 — первый запуск\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
}

_ask_token() {
  osascript -e 'text returned of (display dialog "Вставь Hugging Face токен (hf_...). Он нужен, чтобы скачать веса модели — токен создаётся на странице Settings → Access Tokens (Read), которая сейчас откроется в браузере вместе со страницей модели, где нужно принять лицензию." with title "MiniMax H3 — Hugging Face токен" default answer "" with hidden answer)' 2>/dev/null
}

# true (код возврата 0), если реально нужна первичная настройка (клон/сборка/
# скачивание весов) — используется в run.sh, чтобы решить, открывать ли видимый
# Terminal перед тем, как что-то делать. Если всё уже на месте — false, и
# run.sh работает тихо в фоне как обычно.
bootstrap_needed() {
  mkdir -p "$ENGINE_DIR" 2>/dev/null
  [ ! -x "$H3_BIN" ] || [ ! -d "$MODEL_DIR/FL2VA" ]
}

bootstrap_engine() {
  mkdir -p "$ENGINE_DIR"

  # 1. исходники h3.c (antirez/h3.c) — если их вообще нет
  if [ ! -d "$H3_SRC" ]; then
    _dialog "Это первый запуск на этой машине — движок h3.c ещё не скачан. Сейчас склоню исходники (antirez/h3.c с GitHub) и соберу — минуту-другую. Прогресс виден в этом окне Terminal."
    if ! git clone https://github.com/antirez/h3.c "$H3_SRC"; then
      _dialog "Не получилось склонировать h3.c — проверь интернет и запусти ярлык ещё раз. (Нужен установленный git — он идёт вместе с Xcode Command Line Tools.)"
      exit 1
    fi
  fi

  # 2. сборка бинарника
  if [ ! -x "$H3_BIN" ]; then
    if ! xcode-select -p >/dev/null 2>&1; then
      _dialog "Для сборки движка нужны Xcode Command Line Tools (компилятор). Сейчас откроется системный установщик — после того как он закончит, запусти этот ярлык ещё раз."
      xcode-select --install
      exit 1
    fi
    _dialog "Собираю движок (make -j8) — минуту-две, дальше без диалогов. Прогресс сборки виден в этом окне Terminal."
    if ! (cd "$H3_SRC" && make -j8); then
      _dialog "Сборка h3.c не удалась. Подробности — в этом окне Terminal, или напиши разработчику."
      exit 1
    fi
  fi

  # 3. веса модели — обязательный минимум это FL2VA (~146GB), Ref2VA/transformer
  # (~64GB) тянется следом для режима с референс-картинками
  if [ ! -d "$MODEL_DIR/FL2VA" ]; then
    _dialog "Нужны веса модели MiniMax H3 (суммарно ~210GB — да, это много, качать не быстро). Для этого нужен бесплатный аккаунт на Hugging Face: на странице модели, которая сейчас откроется, прими лицензию (Community License Agreement), а на странице токенов создай Read-токен. Дальше вставишь его в следующее окно."
    open "https://huggingface.co/MiniMaxAI/MiniMax-H3"
    open "https://huggingface.co/settings/tokens"
    TOKEN="$(_ask_token)"
    if [ -z "$TOKEN" ]; then
      _dialog "Токен не введён — без него веса не скачать. Запусти ярлык ещё раз, когда он будет готов."
      exit 1
    fi
    _dialog "Начинаю скачивание (~210GB — может занять часы, зависит от интернета). Прогресс виден прямо в этом окне Terminal (и пишется в engine/download.log). Когда закончится, сервер запустится сам — просто подожди, диалог появится ещё раз."
    (cd "$ENGINE_DIR" && HF_TOKEN="$TOKEN" python3 "$REPO_ROOT/scripts/download_weights.py" both) 2>&1 | tee -a "$ENGINE_DIR/download.log"
    if [ ! -d "$MODEL_DIR/FL2VA" ]; then
      _dialog "Скачивание не завершилось успешно — подробности в engine/download.log. Проверь токен/интернет и попробуй снова."
      exit 1
    fi
    # text_encoder/video_vae/audio_vae в Ref2VA/ побайтово совпадают с FL2VA/ —
    # симлинкуем вместо повторного скачивания (см. link_ref2va_shared.sh)
    if [ -f "$REPO_ROOT/scripts/link_ref2va_shared.sh" ] && [ ! -e "$MODEL_DIR/Ref2VA/text_encoder" ]; then
      bash "$REPO_ROOT/scripts/link_ref2va_shared.sh" "$MODEL_DIR" >> "$ENGINE_DIR/download.log" 2>&1
    fi
    _dialog "Готово! Веса на месте, сервер сейчас запустится."
  fi
}
