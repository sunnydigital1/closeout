"""Record Closeout's guided tour as an MP4 for the demo video.

The tour plays itself (captions, pointer, clicks) inside the app; this script just
opens it in a 1920x1080 browser, gives it a real mouse, captures every frame and
encodes the result with H.264.

    # 1. Start Closeout in another terminal:  uvicorn app.main:app
    # 2. One-time setup:
    pip install playwright imageio-ffmpeg
    python -m playwright install chromium
    # 3. Record (about three minutes):
    python scripts/record_demo.py                  # demo month + live PayPal sandbox
    python scripts/record_demo.py --demo-only      # demo month only

Writes closeout-demo.mp4 in the current folder. Run the demo month once in your
browser first, so Gemini's answers are cached and the video doesn't wait on them.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        path = shutil.which("ffmpeg")
        if path:
            return path
    sys.exit("ffmpeg not found. Run: pip install imageio-ffmpeg")


async def capture(url: str, frames_dir: Path, width: int, height: int, timeout_s: int) -> tuple[list[tuple[float, Path]], float, float, list]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("Playwright isn't installed. Run: pip install playwright && python -m playwright install chromium")

    frames: list[tuple[float, Path]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
        page = await context.new_page()
        page.on("pageerror", lambda err: print(f"  page error: {err}"))

        # A real mouse for the tour: hover states and chart tooltips behave as they
        # do for a person.
        async def move(_source, x, y):
            await page.mouse.move(x, y, steps=8)

        async def click(_source, x, y):
            await page.mouse.click(x, y)

        await page.expose_binding("__tourMove", move)
        await page.expose_binding("__tourClick", click)

        cdp = await context.new_cdp_session(page)

        async def ack(session_id):
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:  # a frame that arrives as the browser closes
                pass

        def on_frame(params):
            stamp = params.get("metadata", {}).get("timestamp") or time.time()
            path = frames_dir / f"{len(frames):06d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            frames.append((float(stamp), path))
            asyncio.ensure_future(ack(params["sessionId"]))

        cdp.on("Page.screencastFrame", on_frame)
        print(f"Opening {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 90, "maxWidth": width, "maxHeight": height, "everyNthFrame": 1},
        )
        print("Recording the tour (about three minutes)...")
        started = time.time()
        while True:
            done = await page.evaluate("window.__tourDone === true")
            if done:
                break
            if time.time() - started > timeout_s:
                await browser.close()
                sys.exit("The tour didn't finish in time. Is Closeout running, and does the page load?")
            await asyncio.sleep(0.5)
        t0_ms = await page.evaluate("window.__tourT0 || 0")
        marks = await page.evaluate("window.__tourMarks || []")
        await asyncio.sleep(0.4)
        await cdp.send("Page.stopScreencast")
        end = time.time()
        await browser.close()
    return frames, t0_ms / 1000 if t0_ms else 0.0, end, marks


def encode(frames: list[tuple[float, Path]], start: float, end: float, out: Path, fps: int) -> None:
    # Frames can arrive slightly out of order; their timestamps are the truth.
    kept = sorted((t, f) for t, f in frames if not start or t >= start - 0.05)
    if not kept:
        sys.exit("No frames were captured.")
    lines = ["ffconcat version 1.0"]
    for (t, f), nxt in zip(kept, kept[1:] + [(end, None)]):
        duration = max(nxt[0] - t, 0.001)  # real time; the fps filter evens it out
        lines += [f"file '{f.name}'", f"duration {duration:.4f}"]
    lines.append(f"file '{kept[-1][1].name}'")  # concat needs the last file repeated
    frames_dir = kept[0][1].parent
    (frames_dir / "frames.txt").write_text("\n".join(lines) + "\n")
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", "frames.txt",
        "-vf", f"fps={fps},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-movflags", "+faststart",
        str(out.resolve()),
    ]
    print("Encoding the video...")
    subprocess.run(cmd, cwd=frames_dir, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Closeout's guided tour to MP4.")
    parser.add_argument("--url", default="http://localhost:8000", help="where Closeout is running")
    parser.add_argument("--demo-only", action="store_true", help="skip the live PayPal sandbox segment")
    parser.add_argument("--out", default="closeout-demo.mp4")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--speed", type=float, default=1.0, help="play the tour faster (for a quick test)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for the tour to finish")
    parser.add_argument("--params", default="", help="extra tour options, e.g. ask=0&live=reminder")
    args = parser.parse_args()

    mode = "demo" if args.demo_only else "video"
    url = f"{args.url.rstrip('/')}/?tour={mode}"
    if args.speed != 1:
        url += f"&speed={args.speed}"
    if args.params:
        url += "&" + args.params.lstrip("&?")

    frames_dir = Path(tempfile.mkdtemp(prefix="closeout-frames-"))
    try:
        frames, start, end, marks = asyncio.run(capture(url, frames_dir, args.width, args.height, args.timeout))
        out = Path(args.out)
        encode(frames, start, end, out, args.fps)
        # Where each part of the tour starts, in seconds from the start of the video.
        origin = start or frames[0][0]
        cuts = {m["name"]: round(m["at"] / 1000 - origin, 2) for m in marks}
        out.with_suffix(".json").write_text(json.dumps(cuts, indent=1) + "\n")
        length = end - (start or frames[0][0])
        print(f"Saved {out.resolve()} ({length:.0f} seconds, {len(frames)} frames captured)")
        if length > 178:
            print("Note: that's over 3 minutes. Judges may stop watching at 3:00.")
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
