"""Schema / semantic-model drift check — cron/CI runnable, zero LLM.

Compares every registered datasource's **live schema** against two
declared shapes:

  - KB drift (``schema_notes.yml``): new/gone tables, added/removed
    columns (``detect_drift``).
  - Semantic drift (``semantics.yml`` via ``SemanticLayerProvider``):
    declared datasets / fields / keys / relationship endpoints that no
    longer match the live schema.

This is the alerting front door for the runbook: the serve periodic
sweep only *logs* drift, this script turns it into an exit code.

Exit codes (for cron / CI):
  0  clean — no drift, and **every** check actually ran
  1  drift found (alert)
  2  error — no datasources.yml, invalid args, **或任一检查未能完成**

「未能完成」也算 2 是本脚本 2026-09-28 的关键修正。在此之前,catalog 连
不上时 ``detect_drift`` 返回一份全空报告,本脚本据此算出 ``dirty=False``
→ **exit 0 → CI 报绿**。也就是说:**连不上数据库的时候,漂移门禁是最绿的。**
exit 2 的文档语义本来就是「连接失败」,所以这不是新增契约,是把它兑现。

Usage:
    uv run python scripts/check_drift.py [--datasource NAME_OR_URL] [--json]
        [--kb-dir DIR] [--level L1|L2|L3] [--verbose]

``--level`` is repeatable and defaults to L1+L2. L3 (live value anchoring /
relationship additivity) is opt-in: it probes the live DB once per declared
field value-set and once per declared key/relationship, so it is not part of
the default gate.

Datasources come from ``.trove/datasources.yml`` (what serve registered);
``--datasource`` selects one by registered name or a ``scheme://...`` URL.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.core.types import DatasourceConfig
from trove.services.datasource.config_store import ConfigStore
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.drift import DriftService
from trove.services.kb.service import KbService, resolve_kb_root

#: 内置 demo 数据源的名字。它在 ``naming.RESERVED_NAMES`` 里 —— 用户**注册不了**
#: 这个名字(admin API 直接拒),所以注册表里的 ``demo`` 只可能是内置的那个。
DEMO_NAME = "demo"


async def _check_datasource(name: str, cfg, kb, levels: set[str] | None = None) -> dict:
    """跑一次检测。**本函数只做接线**,检测/合流/落库都在 ``DriftService``。

    在 2026-09-28 之前这里自己串了两个检测器,于是同一个「合流」逻辑在
    本脚本与 API 里各有一份;更要紧的是结果**不落库** —— cron 每天跑,
    每天都报同一批名字,分不出「新漂移」与「已知未处理」。走服务层之后
    这两件事一起解决:一份合流实现,``first_seen_at`` / ``seen_count``
    让「昨天就有」与「刚冒出来」在数据上分开。
    """
    registry = ConnectorRegistry()
    try:
        if getattr(cfg, "type", "") == "demo":
            from trove.services.datasource.demo_setup import setup_demo_datasource
            await setup_demo_datasource(registry, set_default=False)
        else:
            await registry.register(cfg)

        async def schema_provider(datasource: str) -> dict[str, set[str]]:
            adapter = await registry.get(datasource)
            schema = await adapter.get_schema()
            return {
                str(t.name).lower(): {str(c.name).lower() for c in t.columns}
                for t in schema.tables
            }

        async def semantic_factory(datasource: str, catalog: dict[str, set[str]]):
            """没有 ``semantics.yml`` → None:该库没有语义层 = **不适用**。

            与「有语义层但读不出来」必须分开 —— 后者会以 skipped 传上来,
            前者不该让一个从没建过语义层的库永远报错。
            """
            path = kb.semantics_path(datasource)
            if not path.exists():
                return None
            from trove.services.semantic_layer.provider import SemanticLayerProvider

            adapter = await registry.get(datasource)
            return SemanticLayerProvider(
                directory=Path.cwd() / ".trove" / "semantic" / datasource,
                datasource=datasource,
                dialect=adapter.dialect() or "sqlite",
                kb_semantics_path=path,
                catalog=catalog,
            )

        async def live_detector(datasource: str, model):
            """L3 只拿 adapter;模型由服务层从同一个 provider 取。"""
            from trove.services.semantic_layer.live_drift import detect_live_drift

            adapter = await registry.get(datasource)
            return await detect_live_drift(
                adapter, model, adapter.dialect() or "sqlite")

        svc = DriftService(Path.cwd(), kb=kb, schema_provider=schema_provider,
                           semantic_factory=semantic_factory,
                           live_detector=live_detector)
        try:
            drift = await svc.detect(name, levels=levels)
        finally:
            await svc.dispose()
        return {"drift": drift}
    finally:
        await registry.close_all()


async def _run(args) -> tuple[dict, int]:
    store = ConfigStore()
    configs = store.load_configs()
    if args.datasource:
        if "://" in args.datasource:
            cfg = parse_datasource_url(args.datasource)
            configs = [cfg]
        else:
            configs = [c for c in configs if c.name == args.datasource]
        if not configs and args.datasource == DEMO_NAME:
            # 内置 demo 不需要先登记。它是**产品的一部分**,不是用户注册的源:
            # 任何一台机器上只要仓库在、KB 在,就该能查它的漂移 —— 包括
            # **从没跑过 serve、因此没有 datasources.yml 的全新 clone**。
            #
            # 这条不是可有可无的便利:CI 恰好就是那种环境(该文件被 gitignore,
            # 只有跑过 serve/admin 注册才会有)。缺了它,漂移门在 CI 上永远是
            # exit 2「没查成」—— 而一份 red 会因为看起来像数据有问题而被
            # 当成噪声,门就此失效。这正是我们把「没查成」与「干净」分开的
            # 反面:分开是为了让失败可读,不是为了让它变成常态。
            #
            # 注册表里有 demo 时上面那行已经取到了,轮不到这里 —— 用登记的那份
            # (带持久化的连接信息)。不会有「顶掉用户自己的 demo」这种情形:
            # ``naming.RESERVED_NAMES`` 让用户注册不了这个名字。
            configs = [DatasourceConfig(name=DEMO_NAME, type="demo")]
        if not configs:
            return {"error": f"datasource not found: {args.datasource}"}, 2

    if not configs:
        return {"error": ".trove/datasources.yml 无注册数据源"}, 2

    kb_root = resolve_kb_root(args.kb_dir, configs[0].name)
    kb = KbService(Path.cwd(), kb_dir=kb_root)

    levels = set(args.level) if args.level else None
    reports = {}
    errors = []
    for cfg in configs:
        try:
            reports[cfg.name] = await _check_datasource(
                cfg.name, cfg, kb, levels)
        except Exception as e:
            errors.append(f"{cfg.name}: {e}")

    return decide(reports, errors)


def decide(reports: dict, errors: list[str]) -> tuple[dict, int]:
    """(payload, exit code) —— 决策与 I/O 分离,好让退出码可被单独测。

    退出码是整个脚本的意义所在(CI 靠它判断)。这条决策此前埋在 ``_run``
    里、没有任何测试覆盖,于是「连不上库 → exit 0」活了很久没人发现。

    **部分判不了也是 2**。L3 请求了、条目也检出来了,但有些声明探不动
    (表达式不是裸列 / 域太大 / 方言不支持):这时报告 ``ok`` 且可能带
    若干条目 —— 若只看「有没有条目」,一次漏掉关键声明的检查会以 exit 1
    (只报漂移)收场,而「判不了」在退出码里根本看不见。判不了 ≠ 通过,
    与「没查成」同级,都归 2。
    """
    dirty = {}
    incomplete = {}
    partial = {}
    for name, rep in reports.items():
        drift = rep["drift"]
        if not drift.ok:
            # 未完成 —— **不得**计入「干净」。这正是修掉的那条:此前空报告
            # 被读成 dirty=False,于是连不上库时 exit 0。
            incomplete[name] = drift.skip_reason or "unspecified"
            continue
        if drift.items:
            dirty[name] = rep
        if drift.unverified:
            partial[name] = [u.to_dict() for u in drift.unverified]

    payload = {"datasources": {n: {"drift": rep["drift"].to_dict()}
                               for n, rep in reports.items()}}
    if incomplete:
        payload["incomplete"] = incomplete
    if partial:
        payload["partial"] = partial
    if errors:
        payload["errors"] = errors

    if errors or incomplete or partial:
        return payload, 2
    return payload, 1 if dirty else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", default=None,
                        help="仅检查指定数据源(已注册名或 scheme:// URL)")
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录;默认 <cwd>/.trove/kb")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON(报警用)")
    parser.add_argument("--level", action="append", choices=["L1", "L2", "L3"],
                        help="可重复;缺省 L1+L2。L3 = 活库探测,不进默认")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result, code = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _render_human(result, set(args.level) if args.level else None)
    return code


def _render_human(result: dict, requested: set[str] | None = None) -> None:
    # 未完成的数据源先单独喊出来。它们的 KB drift 会显示为 OK(因为报告确实
    # 是空的),不先把这层说清楚,读者只会看到一片 OK。
    incomplete = result.get("incomplete") or {}
    if incomplete:
        print("== 检查未完成(不得视为干净) ==", file=sys.stderr)
        for name, reason in sorted(incomplete.items()):
            print(f"  ! {name}: {reason}", file=sys.stderr)

    errors = result.get("errors")
    if errors:
        print("== ERRORS ==", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return
    requested = set(requested) if requested else {"L1", "L2"}
    for name, rep in sorted(result.get("datasources", {}).items()):
        drift = rep["drift"]
        status = drift["status"]
        verified = set(drift.get("levels_verified") or ())
        items = drift.get("items") or []
        by_level = {lv: [i for i in items if i["level"] == lv]
                    for lv in ("L1", "L2", "L3")}
        by_unverified: dict[str, list[dict]] = {}
        for u in drift.get("unverified") or []:
            by_unverified.setdefault(u.get("level"), []).append(u)

        print(f"== {name} ==")

        # 每一级单独说「查了没有」。整体 status=ok 只说明**被请求的那些级**
        # 跑完了 —— 一个只跑了 L1 的 ok 报告对 L2 一无所知,不能渲染成 OK。
        for level, label in (("L1", "KB drift"), ("L2", "Semantic drift"),
                             ("L3", "Live semantics")):
            if status != "ok":
                print(f"{label}: 未检查 ({drift.get('skip_reason')})")
                continue
            if level in verified:
                found = by_level[level]
                print(f"{label}: {'OK' if not found else 'DRIFT'}")
                for i in found:
                    print(_render_item(i))
                continue
            pending = by_unverified.get(level) or []
            if pending:
                # 逐条写出判不了的原因:一句笼统的 PARTIAL 会让人以为
                # 「差一点就查完了」,而真正要修的是这些具体声明。
                print(f"{label}: PARTIAL (判不了 {len(pending)} 项,不得视为通过)")
                for u in pending:
                    print(f"  ? {u.get('subject')}: {u.get('reason')}")
                continue
            if level not in requested:
                print(f"{label}: n/a (not requested)")
            elif level in ("L2", "L3"):
                # 请求了但没有 semantics.yml —— 不适用,不是失败。
                print(f"{label}: n/a (no semantic layer)")
            else:
                print(f"{label}: n/a")


def _render_item(item: dict) -> str:
    """一条漂移证据 → 一行人类可读。

    渲染的是**合流后的统一形态**,不是原检测器的形状 —— 这正是本脚本改用
    ``DriftService`` 的意义:两条路在这里已经相加过了。原字段在 ``detail``
    里,所以渲染仍然精确(``+ new table: x`` 而不是笼统的 ``+ x``)。
    """
    d, kind = item.get("detail") or {}, item["kind"]
    if kind == "table_added":
        return f"  + new table: {d.get('table')}"
    if kind == "table_removed":
        return f"  - gone table: {d.get('table')}"
    if kind == "column_added":
        return f"  + {d.get('table')}.{d.get('column')} added"
    if kind == "column_removed":
        return f"  - {d.get('table')}.{d.get('column')} removed"
    if kind == "dataset_table_missing":
        return f"  - gone dataset: {d.get('dataset')}"
    if kind == "field_column_missing":
        return f"  - {d.get('dataset')} missing field: {d.get('field')}"
    if kind == "key_column_missing":
        return f"  - {d.get('dataset')} missing key: {d.get('key')}"
    if kind == "relationship_broken":
        return f"  - relationship {d.get('relationship')}: {d.get('problems')}"
    if kind == "value_missing":
        missing = d.get("missing") or []
        shown = ", ".join(str(v) for v in missing[:8])
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        return (f"  - {d.get('dataset')}.{d.get('field')} declared value(s) "
                f"missing from live data: {shown}{more}")
    if kind == "key_not_unique":
        return f"  - key not unique: {d.get('target')} — {d.get('message')}"
    if kind == "relationship_not_additive":
        return (f"  - relationship not additive: {d.get('target')}"
                f" — {d.get('message')}")
    if kind == "fan_out_not_deduplicable":
        return (f"  - fan_out dedup precondition broken: {d.get('target')}"
                f" — {d.get('message')}")
    # 未知 kind 也要出得来:静默丢弃等于让检测器的发现消失。
    return f"  - [{item['level']}] {item['subject']} ({kind})"


if __name__ == "__main__":
    sys.exit(main())
