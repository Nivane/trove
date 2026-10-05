#!/usr/bin/env python3
"""Assemble the README hero GIF from the frames record-readme-gif.mjs captured.

    python3 scripts/build_readme_gif.py <frames-dir> <out.gif>

<frames-dir> must hold f*.png (in capture order) + times.json (capture
timestamps, written by the recorder). The pipeline:

- downscale every frame to the GIF's display size (default 1440×900 — the
  recorder's native CSS-pixel size, so the GIF stays sharp on retina displays
  where GitHub renders the README image at 880 CSS px ≈ 1760 device px)
- drop frames whose pixels did not change beyond --eps and fold their elapsed
  time into the previous frame's duration — that is what turns the model's
  "thinking" pauses into natural holds instead of dead frames
- clamp durations, cap total playback, hold the last frame longer
- quantize every frame onto one palette taken from the final (richest) frame

Only dependency: Pillow (`python3 -m pip install pillow`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from PIL import Image, ImageChops


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('frames_dir', help='directory with f*.png + times.json')
    p.add_argument('out', help='output .gif path')
    p.add_argument('--target', default='1440x900', help='output size WxH (default 1440x900, the recorder native size)')
    p.add_argument('--eps', type=int, default=4, help='per-channel diff (0-255) below which a pixel counts as unchanged')
    p.add_argument('--dur-min-ms', type=int, default=110)
    p.add_argument('--dur-max-ms', type=int, default=650)
    p.add_argument('--tail-min-ms', type=int, default=900, help='minimum hold on the final frame')
    p.add_argument('--tail-max-ms', type=int, default=3000)
    p.add_argument('--total-max-ms', type=int, default=16000, help='scale all durations down if playback exceeds this')
    p.add_argument('--stills-dir', default=None, help='optionally write first/mid/last frames as PNG for review')
    return p.parse_args()


def main() -> None:
    args = parse_args()
    target = tuple(int(v) for v in args.target.lower().split('x'))

    times = json.load(open(os.path.join(args.frames_dir, 'times.json')))
    if not times:
        sys.exit('no frames recorded')
    files = [t['file'] for t in times]
    ts = [t['t'] for t in times]

    kept: list[list] = []  # [image, t_start]
    for f, t in zip(files, ts):
        im = Image.open(f).convert('RGB').resize(target, Image.LANCZOS)
        if not kept:
            kept.append([im, t])
            continue
        diff = ImageChops.difference(im, kept[-1][0])
        mask = diff.convert('L').point(lambda v: 255 if v > args.eps else 0)
        if mask.getbbox() is None:
            kept[-1][0] = im  # refresh pixels, keep the start time
        else:
            kept.append([im, t])

    durs = []
    for k in range(len(kept)):
        end = kept[k + 1][1] if k + 1 < len(kept) else ts[-1] + (ts[-1] - ts[-2] if len(ts) > 1 else 800)
        durs.append(end - kept[k][1])

    out_durs = []
    for k, d in enumerate(durs):
        last = k == len(durs) - 1
        lo, hi = (args.tail_min_ms, args.tail_max_ms) if last else (args.dur_min_ms, args.dur_max_ms)
        out_durs.append(max(lo, min(hi, d)))

    total = sum(out_durs)
    if total > args.total_max_ms:
        sc = args.total_max_ms / total
        out_durs = [max(70, int(d * sc)) for d in out_durs]
        out_durs[-1] = max(out_durs[-1], 2000)

    pal = kept[-1][0].quantize(colors=256)  # palette from the final (richest) frame
    p_frames = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im, _ in kept]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    p_frames[0].save(
        args.out,
        save_all=True,
        append_images=p_frames[1:],
        duration=out_durs,
        loop=0,
        optimize=True,
    )

    print(f'gif: {args.out}  {len(kept)}/{len(files)} frames  '
          f'{sum(out_durs) / 1000:.1f}s  {os.path.getsize(args.out) / 1e6:.2f} MB')

    if args.stills_dir:
        os.makedirs(args.stills_dir, exist_ok=True)
        for idx, tag in [(0, 'first'), (len(kept) // 2, 'mid'), (len(kept) - 1, 'last')]:
            kept[idx][0].save(os.path.join(args.stills_dir, f'gif-{tag}.png'))


if __name__ == '__main__':
    main()
