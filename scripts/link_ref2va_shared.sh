#!/usr/bin/env bash
# Ref2VA/text_encoder, Ref2VA/video_vae/source и Ref2VA/audio_vae — это
# побайтово идентичные копии соответствующих файлов из FL2VA/ (проверено
# по sha256 через HF API). Вместо повторного скачивания ~72 ГБ просто
# симлинкуем их. Уникален в Ref2VA/ только transformer (уже скачан) и
# крошечный tokenizer (качается отдельно, см. download_weights.py ref2va).
#
# MODEL_DIR передаётся первым аргументом (по умолчанию — ./MiniMax-H3
# относительно текущей директории) — этот скрипт лежит в отслеживаемой
# scripts/, а не рядом с самими весами (engine/ целиком в .gitignore).
set -euo pipefail
MODEL_DIR="${1:-$(pwd)/MiniMax-H3}"
cd "$MODEL_DIR"

[ -e "Ref2VA/text_encoder" ] || ln -s ../FL2VA/text_encoder Ref2VA/text_encoder
mkdir -p Ref2VA/video_vae
[ -e "Ref2VA/video_vae/source" ] || ln -s ../../FL2VA/video_vae/source Ref2VA/video_vae/source
[ -e "Ref2VA/audio_vae" ] || ln -s ../FL2VA/audio_vae Ref2VA/audio_vae

echo "done:"
ls -la Ref2VA/
