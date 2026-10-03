#!/usr/bin/env python3
"""Build the GitHub social preview card (1280×640) → assets/social-preview.png.

    python3 scripts/build_social_card.py [--shot PATH] [--out PATH]

Crops a product screenshot for the right panel, composes the card (dark theme,
docs-site design tokens), renders it with headless Chrome at 2x and downscales
to the exact 1280×640 GitHub wants. The upload itself is web-UI-only:
repo Settings → General → Social preview.

Requires: Pillow, and a Chrome/Chromium binary (CHROME_BIN env overrides the
macOS default path).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent

# The chat column of the docs' chart screenshot: after the 300px sidebar,
# before the analysis rail (its border sits at ~1114px of the 1440px viewport).
CROP_CSS = (300, 36, 1114, 780)
SHOT_WIDTH_CSS = 1440

CARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; }
  .card {
    width: 1280px; height: 640px; box-sizing: border-box;
    background:
      radial-gradient(90% 130% at 0% 0%, rgba(99,102,241,.22), rgba(99,102,241,0) 55%),
      radial-gradient(70% 100% at 100% 100%, rgba(99,102,241,.10), rgba(99,102,241,0) 60%),
      #09090b;
    display: flex; align-items: center; gap: 40px;
    padding: 64px 56px 64px 72px;
    font-family: "IBM Plex Sans", ui-sans-serif, -apple-system, BlinkMacSystemFont,
                 "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    color: #f4f4f5;
    -webkit-font-smoothing: antialiased;
  }
  .left { flex: 1; min-width: 0; }
  .eyebrow { font-size: 19px; letter-spacing: .12em; color: #a5b4fc; font-weight: 500; margin-bottom: 38px; }
  .brand { display: flex; align-items: flex-end; gap: 18px; }
  .mark { display: flex; align-items: flex-end; gap: 5px; height: 46px; padding-bottom: 6px; }
  .mark i { display: block; width: 11px; border-radius: 3px; background: #6366f1; }
  .mark i:nth-child(1) { height: 22px; opacity: .5; }
  .mark i:nth-child(2) { height: 34px; opacity: .78; }
  .mark i:nth-child(3) { height: 46px; }
  .word { font-size: 74px; font-weight: 700; letter-spacing: -.02em; line-height: 1; }
  .identity { font-size: 34px; font-weight: 600; color: #a5b4fc; margin: 16px 0 26px; }
  .chain { font-size: 25px; color: #d4d4d8; margin-bottom: 28px; }
  .hair { height: 1px; background: #27272a; margin: 0 0 24px; width: 88%; }
  .facts { font-size: 19px; color: #a1a1aa; line-height: 1.65; margin-bottom: 22px; }
  .urls { font-size: 18px; color: #71717a; }
  .urls b { color: #a1a1aa; font-weight: 500; }
  .shot {
    width: 600px; height: 512px; flex: 0 0 auto;
    border-radius: 16px; overflow: hidden;
    border: 1px solid #27272a;
    box-shadow: 0 40px 80px -30px rgba(0,0,0,.75);
    background: #fff;
  }
  .shot img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; }
</style>
</head>
<body>
  <div class="card">
    <div class="left">
      <div class="eyebrow">开源 · Apache-2.0 · 可自托管</div>
      <div class="brand">
        <div class="mark"><i></i><i></i><i></i></div>
        <div class="word">Trove</div>
      </div>
      <div class="identity">数据决策智能体</div>
      <div class="chain">问数 · 分析 · 决策 · 行动，一条对话链路</div>
      <div class="hair"></div>
      <div class="facts">语义优先 NL→SQL · 确定性安全层 · 只读接入你的数据<br>答案与判定都带证据、可核对</div>
      <div class="urls"><b>github.com/Nivane/trove</b> · nivane.github.io/trove</div>
    </div>
    <div class="shot"><img src="card-shot.png"></div>
  </div>
</body>
</html>
"""


def find_chrome() -> str:
    candidates = [
        os.environ.get('CHROME_BIN'),
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        shutil.which('google-chrome'),
        shutil.which('chromium'),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit('no Chrome found — set CHROME_BIN to a Chrome/Chromium binary')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--shot', default=str(REPO / 'docs/assets/shots/user-chat-chart.png'),
                   help='product screenshot to crop (default: the chart answer shot)')
    p.add_argument('--out', default=str(REPO / 'assets/social-preview.png'))
    args = p.parse_args()

    im = Image.open(args.shot)
    sc = im.size[0] / SHOT_WIDTH_CSS
    x0, y0, x1, y1 = (round(v * sc) for v in CROP_CSS)
    crop = im.crop((x0, y0, x1, y1))

    tmp = Path(tempfile.mkdtemp(prefix='trove-card-'))
    try:
        crop.save(tmp / 'card-shot.png')
        (tmp / 'social-card.html').write_text(CARD_HTML, encoding='utf-8')
        shot2x = tmp / 'card@2x.png'
        subprocess.run(
            [
                find_chrome(),
                '--headless=new',
                '--disable-gpu',
                '--hide-scrollbars',
                '--force-device-scale-factor=2',
                '--window-size=1280,640',
                '--virtual-time-budget=2000',
                f'--screenshot={shot2x}',
                (tmp / 'social-card.html').as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.open(shot2x).resize((1280, 640), Image.LANCZOS).save(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'social card: {args.out}  {os.path.getsize(args.out) / 1e3:.0f} KB  '
          '(upload via repo Settings → Social preview)')


if __name__ == '__main__':
    main()
