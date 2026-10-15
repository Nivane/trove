#!/usr/bin/env python3
"""回顾归因 CLI — 历史失败题 vs 当下声明层(零 LLM;--dsn 时只读探测)。

拿一批历史失败(errors/failures.jsonl,自带 gold SQL)去问一件事:
**这些失败里,有多少能被语义模型的声明层解释?** —— 这正是「该不该在声明
层继续投入(补实体/补边/做 A/B)」的那个前置问题,答案是一张分桶表。

  uv run python scripts/eval_attribution.py \
      --failures .trove/eval/failures.jsonl \
      --kb-dir .trove/kb/financial \
      --dsn mysql://root:root@127.0.0.1:3306/financial

--dsn 给了才连库:连上就跑 verify_declarations(自然键/关系「一」侧/dedup
前提的对库证伪),声明前提才有真假;不给就只判**声明覆盖**(哪些表没声明
为数据集、哪些连接不是声明的边),声明过的前提一律计入「判不了」——
**没测过不等于为真**。

--kb-rev 可以用历史版本的 KB 归因(git rev,如 97e9bbb 或 HEAD~3);
不给就用工作区当下的 KB,并在报告里带上 KB 指纹与 `kb_changed_since`
标记(失败记录之后 KB 改过 → 这是反事实诊断,不是当时的原因)。

退出码:0 正常 · 1 --strict 且存在被证伪/被拦路的声明 · 2 用法或输入错误。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from trove.eval.attribution import (
    build_index,
    build_report,
    load_rows,
    render_report,
)
from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.verify import verify_declarations

STRICT_BUCKETS = ("premise_false_walked", "premise_false_needed", "path_blocked")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--failures", default=".trove/eval/failures.jsonl",
                   help="失败条目 JSONL(默认 .trove/eval/failures.jsonl;"
                        "results.jsonl 亦可,它会带出 run_id)")
    p.add_argument("--kb-dir", default=None,
                   help="KB 目录(含 semantics.yml);缺省 = 由 --dsn 的库名推导 "
                        ".trove/kb/<库名>")
    p.add_argument("--kb-rev", default=None,
                   help="用 git 里的历史版本 KB 归因(rev,如 HEAD~3 / 97e9bbb)")
    p.add_argument("--dsn", default=None,
                   help="业务库连接串(给了才连库做声明验证;"
                        "如 mysql://user:pass@host:3306/financial)")
    p.add_argument("--dialect", default=None,
                   help="SQL 解析方言(缺省 = 适配器方言,再退 mysql)")
    p.add_argument("--samples", type=int, default=3,
                   help="每个桶的样例题条数(默认 3)")
    p.add_argument("--strict", action="store_true",
                   help="存在「假前提」或「声明拦路」桶时退出码 1(可挂 CI/提交前)")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--out", default=None, help="把 JSON 报告另存到文件")
    return p.parse_args()


def _git(*argv: str) -> str:
    try:
        out = subprocess.run(("git",) + argv, capture_output=True, text=True,
                             check=True)
    except Exception:  # noqa: BLE001 — 没有 git / 不是仓库:版本信息缺失而已
        return ""
    return out.stdout.strip()


def _repo_root() -> Path:
    top = _git("rev-parse", "--show-toplevel")
    if not top:
        raise RuntimeError("不在 git 仓库里(--kb-rev / KB 指纹需要 git)")
    return Path(top)


def _kb_path(kb_dir: Path | None, dsn: str | None) -> Path:
    if kb_dir is not None:
        return kb_dir / "semantics.yml"
    if dsn:
        from trove.services.datasource.urls import parse_datasource_url

        cfg = parse_datasource_url(dsn)
        return Path(".trove") / "kb" / cfg.name / "semantics.yml"
    raise SystemExit(
        "需要 --kb-dir(或给 --dsn 由库名推导 KB 目录)")


def _load_model_text(path: Path, rev: str | None) -> tuple[str, str]:
    """→ (YAML 文本, 出处描述)。有 --kb-rev 就从 git 取那一版。"""
    if rev:
        rel = path.resolve().relative_to(_repo_root())
        text = _git("show", f"{rev}:{rel.as_posix()}")
        if not text:
            raise SystemExit(f"git 里取不到 {rev}:{rel.as_posix()}")
        return text, f"{rev}:{rel.as_posix()}"
    if not path.exists():
        raise SystemExit(f"KB 文件不存在: {path}")
    return path.read_text(encoding="utf-8"), str(path)


def _kb_provenance(path: Path, rev: str | None, failures: Path) -> dict:
    """KB 指纹 + 与失败记录的时间关系(反事实判定的依据)。"""
    rel = None
    try:
        rel = path.resolve().relative_to(_repo_root()).as_posix()
    except Exception:  # noqa: BLE001 — KB 不在仓库内(本地草稿目录)
        rel = None
    meta: dict = {}
    if rel:
        last = _git("log", "-1", "--format=%h %cI", "--", rel)
        if last:
            meta["kb_last_commit"] = last
            sha, _, when = last.partition(" ")
            if not rev and failures.exists():
                try:
                    committed = _dt.datetime.fromisoformat(when).timestamp()
                    meta["kb_changed_since"] = committed > failures.stat().st_mtime
                except ValueError:
                    pass
    if rev:
        meta["kb_rev"] = rev
    return meta


def _mask_dsn(dsn: str) -> str:
    """报告里不留口令(报告是要给人看、可能落盘的)。"""
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


async def main() -> int:
    args = parse_args()
    failures = Path(args.failures)
    if not failures.exists():
        print(f"失败条目文件不存在: {failures}", file=sys.stderr)
        return 2
    kb_path = _kb_path(Path(args.kb_dir) if args.kb_dir else None, args.dsn)
    text, source = _load_model_text(kb_path, args.kb_rev)
    try:
        model = parse_ossie(text)
    except Exception as e:  # noqa: BLE001
        print(f"语义模型解析失败({source}): {e}", file=sys.stderr)
        return 2

    probes: list = []
    dialect = args.dialect or "mysql"
    live = False
    if args.dsn:
        try:
            probes, probed_dialect, _ds_name, model = await _probe_with_text(
                args.dsn, text)
        except Exception as e:  # noqa: BLE001 — 连不上就响亮失败,不退化
            print(f"连库失败({_mask_dsn(args.dsn)}): {e}", file=sys.stderr)
            return 2
        live = True
        if not args.dialect and probed_dialect:
            dialect = probed_dialect

    index = build_index(model, probes, live=live)
    rows = load_rows(failures)
    meta = {
        "failures": str(failures),
        "failures_mtime": _dt.datetime.fromtimestamp(
            failures.stat().st_mtime).isoformat(timespec="seconds"),
        "kb_source": source,
        "kb_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "dialect": dialect,
        "live": live,
        "datasource": _mask_dsn(args.dsn) if args.dsn else "(未接库)",
    }
    meta.update(_kb_provenance(kb_path, args.kb_rev, failures))
    if live:
        counts: dict[str, int] = {}
        for p in probes:
            counts[p.verdict] = counts.get(p.verdict, 0) + 1
        meta["probes"] = counts

    report = build_report(rows, index, dialect=dialect, meta=meta,
                          samples=args.samples)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(render_report(report))
        if args.out:
            print(f"\n(JSON 报告已写入 {args.out})")

    if args.strict:
        hits = {b: report["counts"]["buckets"].get(b, 0) for b in STRICT_BUCKETS}
        if any(hits.values()):
            print("\n✗ --strict:声明层有被证伪/被拦路的记录 "
                  + "、".join(f"{b}={n}" for b, n in hits.items() if n),
                  file=sys.stderr)
            return 1
    return 0


async def _probe_with_text(dsn: str, text: str):
    """连库探测(用给定的 KB 文本 —— --kb-rev 时探测的是那一版声明)。"""
    from trove.services.datasource.registry import ConnectorRegistry
    from trove.services.datasource.urls import parse_datasource_url

    cfg = parse_datasource_url(dsn)
    registry = ConnectorRegistry()
    try:
        adapter = await registry.register(cfg, set_default=True)
        try:
            dialect = adapter.dialect() or ""
        except Exception:  # noqa: BLE001
            dialect = ""
        model = parse_ossie(text)
        probes = await verify_declarations(adapter, model, dialect)
        return probes, dialect, cfg.name, model
    finally:
        await registry.close_all()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
