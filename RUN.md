# Run — Lite

The engine (`~/MiniMaxH3/engine`, h3 binary + weights) downloads and builds itself automatically
on first run — see "First run" in `README.md`. You don't need anything else set up beforehand.

## Start the web UI

```bash
~/MiniMaxH3/lite/run.sh
```

Runs on http://127.0.0.1:8766.

**Without a terminal:** there's a `Launch MiniMax H3 Lite.app` shortcut next to it — double-click
starts the server and opens the browser on its own, with no dock icon or window (it's just a
background launcher). Log: `server/launcher.log`. Clicking it again while the server is already
running just reopens the browser.

Differences from Pro: reference mode is limited to 3 images instead of 9 (RAM-adaptive presets
with warnings aren't implemented yet — see `PLAN.md` in the repo root, which also has the current
working assumption about the quality/custom presets, not formally restricted).

The "Image" tab (a single image instead of video) is already there, same code as in Pro. On the
first run after this update, `run.sh` will pull in a new dependency (`opencv-python-headless`) —
needs internet access.

⚠️ If you happen to run Pro and Lite at the same time, these are two independent processes — each
has its own internal queue, but they don't know about each other. Running generations in both UIs
at once means two parallel `h3` processes splitting RAM/GPU — the same problem as double-clicking
twice within one edition. Don't run generations in both at the same time.

## If a generation hangs or the server is killed while the GPU is still busy

The server kills its child `h3` process on shutdown (Ctrl+C) and when a job is cancelled via the
"Cancel" button in the UI. But if something went wrong and the process was orphaned:

```bash
ps aux | grep "[h]3 -d"     # find the PID
pkill -9 -f "h3c-src/h3"    # kill it
```
