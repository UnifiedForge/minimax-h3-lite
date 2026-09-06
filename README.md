# MiniMax H3 — Lite

A native macOS GUI for [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) (built on [antirez/h3.c](https://github.com/antirez/h3.c)) — local text-to-video and text-to-image generation, no ComfyUI, no cloud, no API keys. Everything runs on your own Apple Silicon Mac.

![platform](https://img.shields.io/badge/platform-Apple%20Silicon-black) ![license](https://img.shields.io/badge/license-source--available-blue)

## What it does

- Text-to-video and text-to-image generation with h3.c, through a simple web UI (opens in your browser, served locally).
- Speed/quality presets (Draft / Balanced / Quality) plus a Custom mode for manual `steps` / `layers` / `reuse`.
- Reference modes: first/last-frame anchoring, or up to 3 reference images.
- A "Задачи" (Active) / "История" (History) view — history survives server and browser restarts: it's rebuilt from what's already in `outputs/` and `server/jobs/`, so nothing generated is lost between sessions.
- A one-click `.app` launcher — no terminal required for day-to-day use.

## Requirements

- An Apple Silicon Mac (M-series). This is not going to work on Intel Macs or non-Apple hardware.
- **~210 GB of free disk space** for the model weights (see "First run" below) — no partial-download option, plan accordingly.
- A free [Hugging Face](https://huggingface.co) account, to accept the model's license and generate a read token.
- Xcode Command Line Tools (only needed to build the engine on first run; the launcher will prompt you to install them if missing).
- An internet connection for the one-time setup (cloning/building the engine, downloading weights). Generation itself is fully offline afterward.

## Quick start

1. `git clone` this repository (or download it as a ZIP and unzip it).
2. Double-click `Launch MiniMax H3 Lite.app`.

The app isn't code-signed, so macOS will refuse to open it the first time ("Apple could not verify..."). Right-click (or Control-click) it and choose **Open** instead of double-clicking — that shows an "Open Anyway" option double-click doesn't. You only need to do this once.

That's it for day-to-day use. **First run is different** — see below.

### First run

On a fresh clone, the launcher has nothing to work with yet: no `h3.c` engine, no compiled binary, no model weights. The first double-click walks you through all of it with a few plain dialog boxes (a visible Terminal window stays open in the background so you can see it isn't just hanging):

1. Clones and builds `h3.c` (a minute or two).
2. Opens the Hugging Face pages for the model license and your access token, then asks you to paste the token in.
3. Downloads the model weights (~210 GB — this is the slow part, easily hours depending on your connection).

Once that's done, the server starts and your browser opens automatically. Every run after that is instant — the launcher just checks whether the server's already up and opens the browser, or starts it in the background if not.

## Lite vs. Pro

This is the free, source-available edition. A couple of things are intentionally limited compared to the Pro version:

- Reference images: up to 3 per generation (Pro: up to 9).
- No prompt-enhancer (an LLM-assisted prompt expansion feature, Pro-only).

Everything else — presets, both generation modes, the first/last-frame anchor mode, history — is the same engine and the same UI.

## Troubleshooting

`RUN.md` has more detail on running from the terminal directly and on recovering a stuck generation (leftover `h3` process still holding the GPU after a crash or force-quit).

## License

Source-available — see [`LICENSE.md`](./LICENSE.md). In short: you can view, run, and modify your own copy for personal use; you may not redistribute, sell, or resell it. The model weights and the `h3.c` engine are not bundled here and remain under their own respective licenses (downloaded separately by the setup step above).

## Credits

- [antirez/h3.c](https://github.com/antirez/h3.c) — the inference engine this wraps.
- [MiniMaxAI/MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) — the model weights.
