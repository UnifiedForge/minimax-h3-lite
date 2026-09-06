#!/usr/bin/env python3
"""Downloads the MiniMax-H3 checkpoint trees h3.c needs, straight from the
huggingface_hub Python API (avoids CLI-module path differences across
huggingface_hub versions, which broke the old download_weights.sh).

Usage:
    export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python3 download_weights.py both     # FL2VA + Ref2VA/transformer (~210 GB)
    python3 download_weights.py fl2va    # only FL2VA (~146 GB)
    python3 download_weights.py ref2va   # only Ref2VA/transformer (~64 GB)

Downloads into $H3_ENGINE_DIR/MiniMax-H3 if H3_ENGINE_DIR is set, otherwise
into ./MiniMax-H3 (relative to the current directory) — this script lives in
the repo's tracked scripts/ folder, not inside engine/ itself (engine/ is
gitignored: 200+GB of weights don't belong in git), so it can't locate the
engine directory from its own file path the way it used to.
"""
import os
import sys
import subprocess

MODE = sys.argv[1] if len(sys.argv) > 1 else "both"
if MODE not in ("fl2va", "ref2va", "both"):
    sys.exit(f"unknown mode: {MODE} (use fl2va | ref2va | both)")

TOKEN = os.environ.get("HF_TOKEN")
if not TOKEN:
    sys.exit("ERROR: set HF_TOKEN first (export HF_TOKEN=hf_xxx...)")

ENGINE_DIR = os.environ.get("H3_ENGINE_DIR") or os.getcwd()
MODEL_DIR = os.path.join(ENGINE_DIR, "MiniMax-H3")

# Make sure huggingface_hub + the fast hf_transfer backend are installed
# for the *same* python that is running this script.
try:
    import huggingface_hub  # noqa: F401
except ImportError:
    pkg = "huggingface_hub[hf_transfer]"
    for extra_args in ([], ["--break-system-packages"], ["--user"]):
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-U", pkg] + extra_args
            )
            break
        except subprocess.CalledProcessError:
            continue
    else:
        sys.exit(
            "Could not pip-install huggingface_hub automatically. Run manually:\n"
            f"  {sys.executable} -m pip install --break-system-packages "
            "-U 'huggingface_hub[hf_transfer]'"
        )

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

from huggingface_hub import snapshot_download  # noqa: E402

REF2VA_PATTERNS = [
    # text_encoder/video_vae/audio_vae under Ref2VA/ are byte-identical
    # (verified via HF sha256) to their FL2VA/ counterparts -> symlinked by
    # link_ref2va_shared.sh instead of downloaded again. Only the transformer
    # and the tiny tokenizer are actually distinct/required here.
    "Ref2VA/tokenizer/*",
    "Ref2VA/transformer/*",
]

PATTERNS = {
    "fl2va": ["FL2VA/*"],
    "ref2va": REF2VA_PATTERNS,
    "both": ["FL2VA/*"] + REF2VA_PATTERNS,
}

for pattern in PATTERNS[MODE]:
    print(f"=== downloading pattern: {pattern} ===", flush=True)
    snapshot_download(
        repo_id="MiniMaxAI/MiniMax-H3",
        repo_type="model",
        local_dir=MODEL_DIR,
        token=TOKEN,
        allow_patterns=[pattern],
        max_workers=8,
    )

print("=== done. verify with: ===")
print(f'cd "{ENGINE_DIR}/h3c-src" && ./h3 --info -d "{MODEL_DIR}"')
