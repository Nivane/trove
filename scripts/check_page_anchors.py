#!/usr/bin/env python3
"""校验文档站（docs/**/*.html）里的 file:line 锚点是否仍指向同一处代码。

能力站点把每条能力锚到 Trove 源码的具体行（`compiler.py:634` → GitHub blob#L634）。
源码一改行号就漂，而漂掉的链接比没有链接更糟——读者点过去看到的是别的代码。
这个脚本按「基准提交里那一行的原文」去目标版本里找同一行：找得到就报（或改写）
新行号，找不到就报 UNRESOLVED 交给人处理。

四类问题，各自的抓法不同：
  漂移   基准里有、目标里换了位置 —— 按行文比对，可 --write 自动改写
  失效   基准里那一行的原文在目标里已找不到（代码被删改）—— 只能人工处理
  存疑   锚点里的符号名没出现在所引的那一行 —— 专抓「出生即写错」
  死链   站内 href/src 指向的文件不存在 —— 页面改名/搬家后的典型腐坏

「存疑」这一类是因为前两类**抓不到它**：新写的锚点若基准与当前相同，比对恒等、
永远报告干净——错的行号会一直错下去。所以另做一次内容核对，见 check_file。

    python3 scripts/check_page_anchors.py             # 只检查；有问题则退出码 1
    python3 scripts/check_page_anchors.py --write     # 就地改写行号与两个结构性计数
    python3 scripts/check_page_anchors.py --at HEAD   # 指定比对版本（默认 main）

默认比对 main 而不是 origin/main：页面链接写的是 blob/main/...，也就是**会被发布的
那条分支**。本地 main 领先 origin 时（提交后未推送），拿 origin/main 比对会把按新
代码写的行号误报成漂移。

基准提交逐页记在 HTML 里：`anchor-base: <sha>`。--write 会连同行号一起把它更新到
目标版本 —— 这两件事必须同步：只改行号不改基准，下次再跑就会拿已经改好的行号去旧
基准里取原文，把锚点改到完全错误的位置。

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


_BLOB_CACHE: dict[tuple[str, str, str], list[str] | None] = {}


def blob(repo: Path, rev: str, path: str) -> list[str] | None:
    """某个版本下某个文件的全部行；文件不存在返回 None。

    带缓存：全站 20+ 页会反复引用同一批文件（graphs.py、main.py…），
    每次都 fork 一个 git 进程纯属浪费。
    """
    key = (str(repo), rev, path)
    if key not in _BLOB_CACHE:
        r = git(repo, "show", f"{rev}:{path}")
        _BLOB_CACHE[key] = r.stdout.splitlines() if r.returncode == 0 else None
    return _BLOB_CACHE[key]


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
        # 方言数认两种写法,因为注册方式在 2026-10 变过:
        #   旧 —— `_ADAPTER_REGISTRY = { "sqlite": SQLiteAdapter, ... }` 字面量;
        #   新 —— 模块末尾 `register_adapter("sqlite", SQLiteAdapter)` 自注册。
        # 两种都数,是为了让这个校验器在**新旧两个版本**上取数都正确:
        # 本地默认 `--at main`(合流前仍是旧写法)不会误报「6 → 0」。
        inside = False
        n = 0
        for ln in lines:
            if "_ADAPTER_REGISTRY" in ln and "{" in ln:
                # 空表字面量(`= {}`,新写法的内部状态)不进入字面量模式
                inside = not ln.rstrip().endswith("{}")
                continue
            if inside:
                if ln.strip().startswith("}"):
                    inside = False
                    continue
                if re.match(r'\s*"\w+":\s*\w+Adapter', ln):
                    n += 1
                continue
            if re.match(r'\s*register_adapter\(\s*"\w+"\s*,\s*\w+Adapter\s*\)', ln):
                n += 1
        counts["adapters"] = n
    return counts


LINK_RE = re.compile(r'(?:href|src)="(?!#|https?:|mailto:|//)([^"#?]+)(?:[#?][^"]*)?"')


def check_links(doc: Path, html: str) -> list[tuple[str, str]]:
    """站内链接与静态资源是否都存在。

    文档站最典型的腐坏方式不是锚点漂移，是**死链**——页面改名、搬家、删页之后，
    指向它的链接还留在别处，而且没人会发现。所以和锚点一样纳入校验。

    只查相对路径；外链（http/https/mailto）与纯锚点（#）跳过，它们不由本仓库保证。
    """
    bad: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in LINK_RE.finditer(html):
        target = m.group(1).strip()
        # 同一个目标常在一页里出现多次（导航、正文、页脚各一次），
        # 报一次就够 —— 列表要能当待办用，重复项只会稀释它。
        if not target or target in seen:
            continue
        seen.add(target)
        dest = (doc.parent / target).resolve()
        if not dest.exists():
            bad.append((target, "目标不存在"))
    return bad


def check_file(
    doc: Path, repo: Path, at: str, write: bool, counts: dict[str, int | None]
) -> tuple[list, list, list, list, list, str | None]:
    """校验单个页面。

    返回 (漂移, 失效, 计数漂移, 死链, 存疑, 改写后的 HTML 或 None)。
    """
    html = doc.read_text(encoding="utf-8")

    base = ""
    m = BASE_RE.search(html)
    if m:
        base = m.group(1)

    drifted: list[tuple[str, int, int]] = []
    unresolved: list[tuple[str, int, str]] = []
    suspect: list[tuple[str, int, str]] = []
    resolved: dict[tuple[str, int], int | None] = {}
    reported: set[tuple[str, int]] = set()
    anchors = list(ANCHOR_RE.finditer(html))

    # 有锚点但没写 anchor-base：无从比对，报出来而不是猜一个基准。
    if anchors and not base:
        unresolved.append(("(整页)", 0, "有锚点但缺 anchor-base，无法比对"))
        return drifted, unresolved, [], [], suspect, None

    # 锚点「出生即写错」是上面那套基准比对抓不到的：新写的锚点若基准与当前一致，
    # 比对恒等、永远报告干净。所以另做一次**内容核对**——ref-line 里是纯标识符
    # （ASCII 名字）时，要求它确实出现在所引用的那一行里。中文描述无法自动判断，
    # 跳过而非猜测。
    for m in anchors:
        sym_m = re.search(r'<span class="ref-line">([^<]*)</span>', m.group("body"))
        if not sym_m:
            continue
        sym = sym_m.group(1).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sym):
            continue
        path, line = m.group("path"), int(m.group("line"))
        dst = blob(repo, at, path)
        if dst is None or not (0 < line <= len(dst)):
            continue  # 交给上面的基准比对去报
        if sym not in dst[line - 1]:
            suspect.append((path, line, sym))

    for m in anchors:
        path, old = m.group("path"), int(m.group("line"))

        key = (path, old)
        if key in resolved:
            new = resolved[key]
        else:
            new = None
            src = blob(repo, base, path)
            dst = blob(repo, at, path)
            if src is not None and dst is not None and 0 < old <= len(src):
                needle = src[old - 1].strip()
                if needle:
                    hits = [i + 1 for i, ln in enumerate(dst) if ln.strip() == needle]
                    # 同一段文字常在一处以上（嵌套调用、同一常量在多分支里重复），
                    # 缩进不同但 strip 后一致。早先只取 hits[0] 会把本来正确的
                    # 锚点报成漂移：graphs.py 的 `tool_timeout_s=20.0,` 在 275 与
                    # 980 各有一份，980 被误判成「漂到了 275」——而如果真按它改写，
                    # 就把对的锚点改到错的为止。
                    if old in hits:
                        # 所引那一行的文字现在仍然对得上 ⇒ 没漂。最保守也最正确：
                        # 若 dst[old-1] 正是这句，内容就还在原处，不存在漂移。
                        new = old
                    elif len(hits) == 1:
                        new = hits[0]
                    elif hits:
                        unresolved.append(
                            (path, old, f"{needle}（目标里有 {len(hits)} 行同文，无法确定是哪一处）")
                        )
                    else:
                        unresolved.append((path, old, needle))
                else:
                    unresolved.append((path, old, "(空行或纯空白，无法定位)"))
            else:
                unresolved.append((path, old, "(基准或目标版本里没有该文件/该行)"))
            resolved[key] = new

        # 同一 (path, old) 可能被页面引用多次（如 compiler.py:634 出现两处），
        # 报告去重，只报一次。注意：**只在报告处去重**，改写必须逐处做。
        if new is None or new == old or key in reported:
            continue
        reported.add(key)
        drifted.append((path, old, new))

    # 改写必须**逐处**做。早先的写法是「按 (path, old) 去重后 out.replace(tag, ...)」，
    # 但 replace 只匹配完全相同的标签串，而同一行的两处引用可见文字往往不同
    # （一处写 compiler.py:634、另一处写别的），第二处就被漏掉、留下陈旧锚点 ——
    # 恰恰是这个脚本要防的东西。改成对每个锚点独立改写。
    out = html
    base_used = base
    if write and drifted:
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
        new_base = git(repo, "rev-parse", "--short", at).stdout.strip()
        if new_base and new_base != base:
            out = out.replace(base, new_base)
            base = new_base

    # ── 结构性计数（只有首页有；其余页面没有即跳过）──
    count_drift: list[tuple[str, int, int]] = []
    for m in COUNT_RE.finditer(html):
        kind, num = m.group("kind"), int(m.group("num"))
        actual = counts.get(kind)
        if actual is not None and actual != num:
            count_drift.append((kind, num, actual))
            if write:
                out = out.replace(m.group(0), m.group(0).replace(f">{num}<", f">{actual}<"))

    dead = check_links(doc, html)

    changed = out if (write and (drifted or count_drift)) else None
    return drifted, unresolved, count_drift, dead, suspect, changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs-dir", default=str(ROOT / "docs"), help="文档站根目录")
    ap.add_argument("--repo", default=str(ROOT))
    # 默认比对 main 而不是 origin/main：页面里的链接写的是 blob/main/...
    # 也就是**会被发布的那条分支**。本地 main 领先 origin 时（提交后未推送），
    # 拿 origin/main 比对会把「按新代码写的行号」误报成漂移。
    ap.add_argument("--at", default="main", help="比对的目标版本")
    ap.add_argument("--write", action="store_true", help="就地改写漂移的行号")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser()
    if not (repo / ".git").exists():
        print(f"× 不是 git 仓库: {repo}", file=sys.stderr)
        return 2
    if git(repo, "rev-parse", "--verify", args.at).returncode != 0:
        print(f"× 版本不存在: {args.at}", file=sys.stderr)
        return 2

    docs_dir = Path(args.docs_dir)
    pages = sorted(p for p in docs_dir.rglob("*.html") if "_probe" not in p.name)
    if not pages:
        print(f"× {docs_dir} 下没有 HTML", file=sys.stderr)
        return 2

    counts = structural_counts(repo, args.at)
    label = {"nodes": "主图节点", "adapters": "方言适配器"}

    total_anchors = total_bad = 0
    dirty: list[tuple[Path, str]] = []

    for doc in pages:
        rel = doc.relative_to(docs_dir)
        drifted, unresolved, count_drift, dead, suspect, changed = check_file(
            doc, repo, args.at, args.write, counts
        )
        total_anchors += len(ANCHOR_RE.findall(doc.read_text(encoding="utf-8")))
        bad = (
            len(drifted)
            + len(unresolved)
            + len(count_drift)
            + len(dead)
            + len(suspect)
        )
        total_bad += bad
        if not bad:
            continue

        print(f"\n{rel}")
        for path, old, new in drifted:
            print(f"  漂移  {path}:{old} → :{new}")
        for path, old, why in unresolved:
            print(f"  失效  {path}:{old}  {why}")
        for kind, old, new in count_drift:
            print(f"  计数  {label.get(kind, kind)}: {old} → {new}")
        for path, line, sym in suspect:
            print(f"  存疑  {path}:{line}  该行未见 `{sym}`（锚点可能出生即写错）")
        for target, why in dead:
            print(f"  死链  {target}  {why}")

        if changed is not None:
            doc.write_text(changed, encoding="utf-8")
            dirty.append((rel, f"{len(drifted)} 处行号、{len(count_drift)} 处计数"))

    print(f"\n{len(pages)} 个页面  ·  {total_anchors} 处锚点  ·  比对 {args.at}")
    if not total_bad:
        print("  ✓ 锚点与站内链接全部有效")
        return 0

    if dirty:
        print(f"\n已改写 {len(dirty)} 个文件：")
        for rel, what in dirty:
            print(f"  {rel}（{what}）")
    if not args.write:
        print("\n加 --write 就地改写行号与计数（失效锚点、死链仍需人工处理）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
