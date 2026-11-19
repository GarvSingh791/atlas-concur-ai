"""Assemble docs/assets/atlas-demo.gif (+ poster) from the frames capture.mjs wrote.

Usage: python make_gif.py FRAMES_DIR OUT.gif POSTER.png [width] [colors] [hold_scale]
"""

from __future__ import annotations

import json
import os
import sys

from PIL import Image

frames_dir, out_gif, out_poster = sys.argv[1:4]
width = int(sys.argv[4]) if len(sys.argv) > 4 else 960
colors = int(sys.argv[5]) if len(sys.argv) > 5 else 96
scale = float(sys.argv[6]) if len(sys.argv) > 6 else 1.0

manifest = json.load(open(os.path.join(frames_dir, "manifest.json")))
frames, durations = [], []
for item in manifest:
    im = Image.open(os.path.join(frames_dir, item["file"])).convert("RGB")
    h = round(im.height * width / im.width)
    im = im.resize((width, h), Image.LANCZOS)
    frames.append(
        im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    )
    durations.append(int(item["hold"] * scale))

frames[0].save(
    out_gif,
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=True,
    disposal=1,
)

# Poster: the planner with recommended options (a frame already in the GIF).
poster_src = next(i["file"] for i in manifest if "planner-options" in i["file"])
Image.open(os.path.join(frames_dir, poster_src)).convert("RGB").resize(
    (width, round(900 * width / 1440)), Image.LANCZOS
).save(out_poster, optimize=True)

print(
    f"{out_gif}: {os.path.getsize(out_gif) / 1e6:.2f} MB, {len(frames)} frames, "
    f"{sum(durations) / 1000:.0f}s"
)
