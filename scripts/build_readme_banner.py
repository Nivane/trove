#!/usr/bin/env python3
"""Build the README banner (1280×600 @2x) → assets/banner.png.

    python3 scripts/build_readme_banner.py [--shot PATH] [--out PATH]

Composes the dark poster that heads both READMEs — wordmark + identity pill,
the positioning line, capability chips, and a framed product screenshot with
a floating "analysis steps" chip — and renders it with headless Chrome at 2x.
GitHub shows README images at ~880px wide, so 1280 CSS px carries the extra
sharpness without wasting bytes.

Web fonts load from Google Fonts (the same stack the docs site uses); an
offline run falls back to system fonts and still composes correctly.

Requires: a Chrome/Chromium binary (CHROME_BIN env overrides the macOS default).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BANNER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;450;500;600&display=swap">
<style>
  html, body { width: 1280px; height: 600px; overflow: hidden; }
  body {
    margin: 0; background: #09090b; position: relative;
    font-family: "IBM Plex Sans", ui-sans-serif, -apple-system, BlinkMacSystemFont,
                 "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .glow { position: absolute; left: -140px; top: -260px; width: 1000px; height: 800px;
          background: radial-gradient(50% 50% at 50% 50%, rgba(99,102,241,.22), transparent 70%); }
  .gridbg { position: absolute; inset: 0;
            background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
                              linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
            background-size: 44px 44px;
            -webkit-mask-image: radial-gradient(90% 90% at 26% 30%, #000 20%, transparent 78%);
            mask-image: radial-gradient(90% 90% at 26% 30%, #000 20%, transparent 78%); }
  .halo { position: absolute; right: -120px; top: 60px; width: 700px; height: 520px;
          background: radial-gradient(50% 55% at 50% 50%, rgba(99,102,241,.30), transparent 70%);
          filter: blur(50px); }

  .wrap { position: relative; height: 100%; display: flex; align-items: center; padding: 0 0 0 68px; }
  .left { width: 560px; flex: none; position: relative; z-index: 2; }

  .brand { display: flex; align-items: baseline; gap: 16px; margin-bottom: 22px; }
  .wordmark { font-family: "Familjen Grotesk", sans-serif; font-weight: 700; font-size: 44px; letter-spacing: -.015em; color: #fff; }
  .wordmark .tld { color: #818cf8; }
  .brand .sub { font-family: "IBM Plex Mono", monospace; font-size: 13px; letter-spacing: .1em; color: #a1a1aa;
                border: 1px solid rgba(255,255,255,.14); border-radius: 999px; padding: 6px 14px; background: rgba(255,255,255,.035); }

  .chain { font-family: "IBM Plex Mono", monospace; font-size: 13px; letter-spacing: .18em; color: #a5b4fc; margin: 0 0 18px; }
  .line { font-size: 16.5px; line-height: 1.75; color: #a1a1aa; margin: 0 0 26px; max-width: 30em; }
  .line b { color: #e4e4e7; font-weight: 600; }

  .chips { display: flex; gap: 10px; }
  .chip { font-family: "IBM Plex Mono", monospace; font-size: 11.5px; color: #8b8b94;
          border: 1px solid rgba(255,255,255,.11); border-radius: 7px; padding: 6px 12px; background: rgba(255,255,255,.03); }
  .chip .ok { color: #4ade80; }

  .shot { position: absolute; top: 78px; left: 620px; width: 700px; z-index: 1; }
  .frame { border-radius: 13px; border: 1px solid rgba(255,255,255,.11); background: #101013; overflow: hidden;
           box-shadow: 0 60px 140px -40px rgba(0,0,0,.95); }
  .frame .bar { display: flex; align-items: center; height: 37px; padding: 0 14px; border-bottom: 1px solid rgba(255,255,255,.07); position: relative; background: rgba(255,255,255,.02); }
  .frame .d { width: 10px; height: 10px; border-radius: 50%; background: #3f3f46; margin-right: 6px; }
  .frame .url { position: absolute; left: 50%; transform: translateX(-50%); font-family: "IBM Plex Mono", monospace; font-size: 11px; color: #71717a; }
  .frame img { width: 100%; display: block; }

  .float { position: absolute; left: 560px; top: 268px; z-index: 3; font-family: "IBM Plex Mono", monospace; font-size: 11.5px;
            color: #c7d2fe; background: rgba(30,27,75,.92); border: 1px solid rgba(129,140,248,.4); border-radius: 8px;
            padding: 8px 14px; box-shadow: 0 18px 40px -18px rgba(0,0,0,.9); }
</style>
</head>
<body>
  <div class="glow"></div>
  <div class="gridbg"></div>
  <div class="halo"></div>

  <div class="wrap">
    <div class="left">
      <div class="brand">
        <span class="wordmark">Trove<span class="tld">.</span></span>
        <span class="sub">数据决策智能体</span>
      </div>
      <p class="chain">问数 · 分析 · 决策 · 行动</p>
      <p class="line">自然语言进，<b>验证过的答案</b>出——语义模型划定边界，确定性护栏兜底，只把真正需要模型的那一段交给 LLM。</p>
      <div class="chips">
        <span class="chip"><span class="ok">●</span> Apache-2.0</span>
        <span class="chip">Python 3.12+</span>
        <span class="chip">7400+ tests</span>
        <span class="chip">LangGraph</span>
        <span class="chip">可自托管</span>
      </div>
    </div>

    <div class="shot">
      <div class="frame">
        <div class="bar"><span class="d"></span><span class="d"></span><span class="d"></span><span class="url">nivane.github.io/trove</span></div>
        <img src="__SHOT_URI__" alt="">
      </div>
    </div>
    <span class="float">分析过程 · 13 步 · 11.0s · 校验 OK</span>
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
    p.add_argument('--shot', default=str(REPO / 'docs/assets/shots/user-chat-analysis.png'),
                   help='product screenshot for the frame (default: the chat analysis shot)')
    p.add_argument('--out', default=str(REPO / 'assets/banner.png'))
    args = p.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix='trove-banner-'))
    try:
        html = BANNER_HTML.replace('__SHOT_URI__', Path(args.shot).resolve().as_uri())
        (tmp / 'banner.html').write_text(html, encoding='utf-8')
        shot2x = tmp / 'banner@2x.png'
        subprocess.run(
            [
                find_chrome(),
                '--headless=new',
                '--disable-gpu',
                '--hide-scrollbars',
                '--force-device-scale-factor=2',
                '--window-size=1280,600',
                # 让 webfonts 有时间到位再截图（虚拟时间预算覆盖网络等待）
                '--virtual-time-budget=5000',
                f'--screenshot={shot2x}',
                (tmp / 'banner.html').as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shot2x, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'README banner: {args.out}  {os.path.getsize(args.out) / 1e3:.0f} KB')


if __name__ == '__main__':
    main()
