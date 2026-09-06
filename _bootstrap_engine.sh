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
  osascript -e "display dialog \"$1\" with title \"MiniMax H3 — First run\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
}

_ask_token() {
  osascript -e 'text returned of (display dialog "Paste your Hugging Face token (hf_...). It'"'"'s needed to download the model weights — create it on the Settings -> Access Tokens (Read) page, which will open now in the browser together with the model page where you need to accept the license." with title "MiniMax H3 — Hugging Face token" default answer "" with hidden answer)' 2>/dev/null
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
    _dialog "This is the first run on this machine — the h3.c engine isn't downloaded yet. Cloning the source (antirez/h3.c from GitHub) and building it now — a minute or two. Progress is visible in this Terminal window."
    if ! git clone https://github.com/antirez/h3.c "$H3_SRC"; then
      _dialog "Failed to clone h3.c — check your internet connection and run the launcher again. (Requires git, which comes with Xcode Command Line Tools.)"
      exit 1
    fi
  fi

  # 2. сборка бинарника
  if [ ! -x "$H3_BIN" ]; then
    if ! xcode-select -p >/dev/null 2>&1; then
      _dialog "Building the engine requires Xcode Command Line Tools (the compiler). The system installer will open now — once it finishes, run this launcher again."
      xcode-select --install
      exit 1
    fi
    _dialog "Building the engine (make -j8) — a minute or two, no more dialogs after this. Build progress is visible in this Terminal window."
    if ! (cd "$H3_SRC" && make -j8); then
      _dialog "Building h3.c failed. Details are in this Terminal window — or reach out to the developer."
      exit 1
    fi
  fi

  # 3. веса модели — обязательный минимум это FL2VA (~146GB), Ref2VA/transformer
  # (~64GB) тянется следом для режима с референс-картинками
  if [ ! -d "$MODEL_DIR/FL2VA" ]; then
    _dialog "The MiniMax H3 model weights are needed (~210GB total — yes, that's a lot, and it won't be quick). This requires a free Hugging Face account: on the model page that will open now, accept the license (Community License Agreement), then create a Read token on the tokens page. You'll paste it into the next window."
    open "https://huggingface.co/MiniMaxAI/MiniMax-H3"
    open "https://huggingface.co/settings/tokens"
    TOKEN="$(_ask_token)"
    if [ -z "$TOKEN" ]; then
      _dialog "No token entered — the weights can't be downloaded without one. Run the launcher again once you have it."
      exit 1
    fi
    _dialog "Starting the download (~210GB — this can take hours depending on your internet). Progress is visible right in this Terminal window (and logged to engine/download.log). Once it finishes, the server will start on its own — just wait, another dialog will appear."
    (cd "$ENGINE_DIR" && HF_TOKEN="$TOKEN" python3 "$REPO_ROOT/scripts/download_weights.py" both) 2>&1 | tee -a "$ENGINE_DIR/download.log"
    if [ ! -d "$MODEL_DIR/FL2VA" ]; then
      _dialog "The download didn't finish successfully — see engine/download.log for details. Check your token/internet and try again."
      exit 1
    fi
    # text_encoder/video_vae/audio_vae в Ref2VA/ побайтово совпадают с FL2VA/ —
    # симлинкуем вместо повторного скачивания (см. link_ref2va_shared.sh)
    if [ -f "$REPO_ROOT/scripts/link_ref2va_shared.sh" ] && [ ! -e "$MODEL_DIR/Ref2VA/text_encoder" ]; then
      bash "$REPO_ROOT/scripts/link_ref2va_shared.sh" "$MODEL_DIR" >> "$ENGINE_DIR/download.log" 2>&1
    fi
    _dialog "Done! The weights are in place, the server is starting now."
  fi
}
