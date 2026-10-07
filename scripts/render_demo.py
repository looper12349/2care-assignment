"""Edit real browser captures into a captioned GIF without altering app results.

Run: uv run --with pillow python scripts/render_demo.py
Source captures stay in ignored .data/demo-frames; only edited media is published.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, default=ROOT / ".data/demo-frames")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/carepath-demo.gif")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "docs/assets/demo-timeline.json").read_text())
    width, banner = 1440, 84
    images, durations = [], []
    for index, scene in enumerate(manifest["scenes"], 1):
        source = args.frames / scene["file"]
        if not source.is_file():
            raise SystemExit(f"Missing real browser capture: {source}")
        with Image.open(source) as captured:
            image = captured.convert("RGB")
        # Remove browser chrome only. Appointment text and scores stay intact.
        image = image.crop((0, manifest["browser_chrome_crop_px"], image.width, image.height))
        height = round(image.height * width / image.width)
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height + banner), "#15363e")
        canvas.paste(image, (0, banner))
        draw = ImageDraw.Draw(canvas)
        draw.text((24, 13), f"{index:02d} / {scene['caption']}", font=font(26), fill="#eef7f3")
        draw.text(
            (24, 51),
            "REAL LOCAL APP CAPTURE  |  SYNTHETIC CLINIC  |  LANGSMITH UPLOADS OFF",
            font=font(13),
            fill="#a8d3c7",
        )
        images.append(canvas)
        durations.append(scene["duration_ms"])
    assert len({image.size for image in images}) == 1, "Capture sizes must match."
    # One palette across scenes keeps text and interface colors consistent.
    swatches = Image.new("RGB", (240, 150 * len(images)))
    for index, image in enumerate(images):
        swatches.paste(image.resize((240, 150)), (0, 150 * index))
    palette = swatches.quantize(colors=256)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.output,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    images[-1].save(args.output.with_name("carepath-preview.png"))
    with Image.open(args.output) as gif:
        assert gif.n_frames == len(frames)
        assert gif.size == images[0].size
        measured_duration = 0
        for index in range(gif.n_frames):
            gif.seek(index)
            measured_duration += gif.info["duration"]
        assert measured_duration == sum(durations)
    print(f"Created {args.output.relative_to(ROOT)}")
    print(f"Verified {len(frames)} scenes, {sum(durations) / 1000:g}s, {images[0].size}.")


if __name__ == "__main__":
    main()
