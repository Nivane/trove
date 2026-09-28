#!/usr/bin/env python3
"""校验 docs/index.html 里的 file:line 锚点是否仍指向同一处代码。

能力站点把每条能力锚到 Trove 源码的具体行（`compiler.py:634` → GitHub blob#L634）。
源码一改行号就漂，而漂掉的链接比没有链接更糟——读者点过去看到的是别的代码。
这个脚本按「基准提交里那一行的原文」去目标版本里找同一行：找得到就报（或改写）
新行号，找不到就报 UNRESOLVED 交给人处理。

    python3 scripts/check_page_anchors.py                  # 只检查；有漂移/失效则退出码 1
    python3 scripts/check_page_anchors.py --write          # 就地改写行号与两个结构性计数
    python3 scripts/check_page_anchors.py --at origin/main # 指定比对版本（默认 origin/main）

基准提交记在 HTML 里：`anchor-base: <sha>`。--write 会连同行号一起把它更新到目标
版本 —— 这两件事必须同步：只改行号不改基准，下次再跑就会拿已经改好的行号去旧基准
里取原文，把锚点改到完全错误的位置。

想让它在提交前提醒一句（**不阻断**提交——这是个多人共用的仓库，别因为页面漂移
挡住别人的正常提交）：

    printf '#!/bin/sh\npython3 "$(git rev-parse --show-toplevel)/scripts/check_page_anchors.py" >/dev/null 2>&1 || echo "⚠ 能力站点锚点漂移，跑 scripts/check_page_anchors.py --write"\n' \\
      > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_SLUG = "Nivane/trove"
ROOT = Path(__file__).resolve().parent.parent  # 仓库根（脚本在 scripts/ 下）

ANCHOR_RE = re.compile(
    r'<a class="ref" href="https://github\.com/'
    + REPO_SLUG.replace("/", r"\/")
    + r'/blob/main/(?P<path>[^"#]+)#L(?P<line>\d+)">(?P<body>.*?)</a>',
    re.S,
)
BASE_RE = re.compile(r"anchor-base:\s*([0-9a-f]{7,40})")
COUNT_RE = re.compile(
    r'<span class="repro-n" data-count="(?P<kind>[a-z]+)">(?P<num>\d+)</span>'
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def blob(repo: Path, rev: str, path: str) -> list[str] | None:
    """某个版本下某个文件的全部行；文件不存在返回 None。"""
    r = git(repo, "show", f"{rev}:{path}")
    return r.stdout.splitlines() if r.returncode == 0 else None


def _false_flags(lines: list[str], def_idx: int) -> set[str]:
    """某个函数的签名里默认值为 False 的 bool 开关（如 clarify）。"""
    flags: set[str] = set()
    for ln in lines[def_idx : def_idx + 20]:
        m = re.match(r"\s*(\w+)\s*:\s*bool\s*=\s*False\b", ln)
        if m:
            flags.add(m.group(1))
        if re.match(r"^\)\s*->", ln):  # 签名结束
            break
    return flags


def _off_by_default(
    lines: list[str], idx: int, false_flags: set[str]
) -> bool:
    """该 add_node 是否被 `if <默认关闭的开关>:` 包住（逐层向外找）。

    只看 `if`/`elif` 的显式开关判断，不看 `else:` —— `if clarify:` 的 else
    分支恰恰是默认**走**的那条（refuse / query_sketch 都在里面）。遇到认不出
    的守卫一律当作「会执行」，宁可多算让人来对，也不要悄悄少算。
    """
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    i = idx - 1
    while i >= 0 and indent > 0:
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i -= 1
            continue
        ind = len(lines[i]) - len(lines[i].lstrip())
        if ind < indent:
            m = re.match(r"(?:if|elif)\s+(\w+)\s*:", s)
            if m and m.group(1) in false_flags:
                return True
            indent = ind
        i -= 1
    return False


def structural_counts(repo: Path, rev: str) -> dict[str, int | None]:
    """页面上两个会随代码演进变动的结构性数字。"""
    counts: dict[str, int | None] = {"nodes": None, "adapters": None}

    # 节点数按**默认配置**算 —— 也就是 `build_graphs(services, checkpointer=...)`
    # 实际建出来的那张图。踩过的坑：把全文件的 add_node 名字去重会得到 28，
    # 多出来的那个是 `clarify`，它只在 `clarify=True` 时挂上，而生产调用
    # （trove/main.py）不传该参数，默认 False。对外报的数字必须是默认口径，
    # 否则读者照着自己跑一遍会对不上。
    # 去重仍然必要：graphs.py 里 add_node 共 49 处，含 gen_sql 子图自己的节点，
    # 以及同一节点在不同分支里的重复（refuse 3 次、query_sketch 2 次）。
    lines = blob(repo, rev, "trove/workflow/graphs.py")
    if lines:
        funcs: dict[str, list[str]] = {}
        def_line: dict[str, int] = {}
        cur: str | None = None
        for i, ln in enumerate(lines):
            m = re.match(r"^(?:async )?def (\w+)", ln)
            if m:
                cur = m.group(1)
                funcs[cur] = []
                def_line[cur] = i
            elif cur is not None:
                funcs[cur].append(ln)
        subgraph = {
            n for n, body in funcs.items()
            if any('add_node("generate"' in x for x in body)
        }
        names: set[str] = set()
        for n, body in funcs.items():
            if n in subgraph:
                continue
            off = _false_flags(lines, def_line[n])
            for j, x in enumerate(body):
                mm = re.search(r'add_node\("([^"]+)"', x)
                if not mm:
                    continue
                # body 相对整文件偏移 1 行（def 行本身不在 body 里）
                if not off or not _off_by_default(lines, def_line[n] + 1 + j, off):
                    names.add(mm.group(1))
        counts["nodes"] = len(names)

    lines = blob(repo, rev, "trove/services/datasource/registry.py")
    if lines:
        inside = False
        n = 0
        for ln in lines:
            if "_ADAPTER_REGISTRY" in ln and "{" in ln:
                inside = True
                continue
            if inside:
                if ln.strip().startswith("}"):
                    break
                if re.match(r'\s*"\w+":\s*\w+Adapter', ln):
                    n += 1
        counts["adapters"] = n
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", default=str(ROOT / "docs" / "index.html"))
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--at", default="origin/main", help="比对的目标版本")
    ap.add_argument("--baseline", default="", help="基准提交（默认从 HTML 读）")
    ap.add_argument("--write", action="store_true", help="就地改写漂移的行号")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser()
    if not (repo / ".git").exists():
        print(f"× 不是 git 仓库: {repo}", file=sys.stderr)
        return 2

    html_path = Path(args.html)
    html = html_path.read_text(encoding="utf-8")

    base = args.baseline
    if not base:
        m = BASE_RE.search(html)
        if not m:
            print("× HTML 里没有 anchor-base，且未传 --baseline", file=sys.stderr)
            return 2
        base = m.group(1)

    if git(repo, "rev-parse", "--verify", args.at).returncode != 0:
        print(f"× 版本不存在: {args.at}", file=sys.stderr)
        return 2

    anchors = list(ANCHOR_RE.finditer(html))
    if not anchors:
        print("× 没找到任何锚点，检查 HTML 结构是否变了", file=sys.stderr)
        return 2

    drifted: list[tuple[str, int, int]] = []
    unresolved: list[tuple[str, int, str]] = []
    resolved: dict[tuple[str, int], int | None] = {}
    reported: set[tuple[str, int]] = set()

    for m in anchors:
        path, old = m.group("path"), int(m.group("line"))

        key = (path, old)
        if key in resolved:
            new = resolved[key]
        else:
            new = None
            src = blob(repo, base, path)
            dst = blob(repo, args.at, path)
            if src is not None and dst is not None and 0 < old <= len(src):
                needle = src[old - 1].strip()
                if needle:
                    hits = [i + 1 for i, ln in enumerate(dst) if ln.strip() == needle]
                    if hits:
                        new = hits[0]
                    else:
                        unresolved.append((path, old, needle))
                else:
                    unresolved.append((path, old, "(空行或纯空白，无法定位)"))
            else:
                unresolved.append((path, old, "(基准或目标版本里没有该文件/该行)"))
            resolved[key] = new

        # 同一 (path, old) 可能被页面引用多次（如 compiler.py:634 出现两处），
        # 报告去重，只报一次。
        if new is None or new == old or key in reported:
            continue
        reported.add(key)
        drifted.append((path, old, new))

    # 改写必须**逐处**做。早先的写法是「按 (path, old) 去重后 out.replace(tag, ...)」，
    # 但 replace 只匹配完全相同的标签串，而同一行的两处引用可见文字往往不同
    # （一处写 compiler.py:634、另一处写别的），第二处就被漏掉、留下陈旧锚点 ——
    # 恰恰是这个脚本要防的东西。改成对每个锚点独立改写。
    out = html
    base_used = base  # 报告里要显示**实际比对用的**基准，不是改写后的新基准
    if args.write and drifted:
        fix = {k: v for k, v in resolved.items() if v is not None and v != k[1]}

        def _rewrite(m: re.Match[str]) -> str:
            old_s = m.group("line")
            new = fix.get((m.group("path"), int(old_s)))
            if new is None:
                return m.group(0)
            tag = m.group(0).replace(f"#L{old_s}", f"#L{new}")
            return re.sub(rf"(?<=[:\w]){old_s}(?=[\s<])", str(new), tag, count=1)

        out = ANCHOR_RE.sub(_rewrite, html)

        # anchor-base 必须与改写**同一步**更新。否则下次再跑，会拿已经改好的
        # 行号去旧基准里取原文，二次改写成完全错误的位置 —— 这个坑我踩过。
        new_base = git(repo, "rev-parse", "--short", args.at).stdout.strip()
        if new_base and new_base != base:
            out = out.replace(base, new_base)
            base = new_base

    # ── 结构性计数 ──
    counts = structural_counts(repo, args.at)
    count_drift: list[tuple[str, int, int]] = []
    for m in COUNT_RE.finditer(html):
        kind, num = m.group("kind"), int(m.group("num"))
        actual = counts.get(kind)
        if actual is not None and actual != num:
            count_drift.append((kind, num, actual))
            if args.write:
                out = out.replace(m.group(0), m.group(0).replace(f">{num}<", f">{actual}<"))

    # ── 报告 ──
    label = {"nodes": "主图节点", "adapters": "方言适配器"}
    print(f"锚点 {len(anchors)} 处  ·  基准 {base_used}  →  {args.at}")

    for path, old, new in drifted:
        print(f"  漂移  {path}:{old} → :{new}")
    for path, old, why in unresolved:
        print(f"  失效  {path}:{old}  {why}")
    for kind, old, new in count_drift:
        print(f"  计数  {label.get(kind, kind)}: {old} → {new}")

    bad = len(drifted) + len(unresolved) + len(count_drift)
    if not bad:
        print("  ✓ 全部锚点仍然指向原处")
        return 0

    if args.write:
        html_path.write_text(out, encoding="utf-8")
        print(f"\n已改写 {html_path}（{len(drifted)} 处行号，{len(count_drift)} 处计数）")
        if unresolved:
            print("注意：失效锚点无法自动修复，需要人工改指或删除：")
            for path, old, why in unresolved:
                print(f"  - {path}:{old}  {why}")
        if base != base_used:
            print(f"anchor-base 已同步更新：{base_used} → {base}")
    else:
        print("\n加 --write 就地改写（失效锚点仍需人工处理）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
