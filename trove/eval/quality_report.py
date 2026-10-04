"""判定质量报告 —— 「判得准不准」的可存档、可复算呈现(纯函数,零 I/O)。

B7 的 ``decision/score.py`` 回答了怎么算;这一层回答**怎么交出去**:
把「判定史 + 行动效果」整理成一份能发、能存、能改天重算出一字不差的
报告(per-datasource 报告 + 全局面 + markdown 呈现)。

三条边界:

  1. **口径唯一权威在 ``decision/score.py``** —— 这里不重写任何指标,
     只做包装与措辞。两个出口各算一遍的后果不是"重复",是**漂移**:
     同一段历史在两个地方给出两个有效率,比没有报告更糟(门/基线
     漂移的同款教训,见 ``eval/gate.py`` 文件头);
  2. **纯派生,可复算** —— 输入是已读出的记录(store 读取、outcome 与
     提案的 join 都留给调用方);``generated_at`` 由调用方给、缺省空串,
     同一输入 → 逐字节同一输出(dict 键序固定);
  3. **不足原因原样透传** —— ``insufficient``(few_verdicts/few_effects/
     no_effects)是 ``score_history`` 的判定,呈现层只翻译措辞、绝不
     把「样本不足」悄悄折成 0 或某个百分比:3 次行动里的 2 次有效
     不是「67% 有效率」,它是「别读这个比率」。
"""

from __future__ import annotations

from typing import Any, Iterable

from trove.services.decision.score import (
    MIN_EFFECTS,
    REV_UNKNOWN,
    rollup,
    score_history,
)

#: 不足原因 → 展示措辞(zh/en)。未知原因原样带出,不吞。
_INSUFFICIENT_WORDS = {
    "zh": {
        "few_verdicts": "判定次数不足",
        "few_effects": "测量次数不足",
        "no_effects": "尚无效果测量",
    },
    "en": {
        "few_verdicts": "too few verdicts",
        "few_effects": "too few measurements",
        "no_effects": "no effect measurements",
    },
}


def build_report(
    verdicts: Iterable[Any] | None,
    effects: Iterable[dict[str, Any]] | None = None,
    *,
    datasource: str = "",
    generated_at: str = "",
    min_effects: int = MIN_EFFECTS,
) -> dict[str, Any]:
    """一个数据源的判定史(+ 可选效果条目)→ 质量报告。

    ``verdicts`` / ``effects`` 的条目形状与 ``score_history`` 一致
    (verdict 记录或轻量 dict;效果条目须调用方先 join 好 rule_id /
    rule_rev —— 那是 I/O 的活)。桶随 ``buckets`` 原样带出,排序由
    ``score_history`` 保证(按 key 升序,确定性)。
    """
    buckets = score_history(verdicts, effects=effects, min_effects=min_effects)
    return {
        "datasource": str(datasource or ""),
        "generated_at": str(generated_at or ""),
        "buckets": buckets,
        "summary": rollup(buckets),
    }


def fleet_report(
    reports: Iterable[dict[str, Any]] | None,
    *,
    generated_at: str = "",
) -> dict[str, Any]:
    """各源报告 → 全局面(计数相加、比率按同一口径重算)。

    跨源汇总只给**总量**:同名规则在两个数据源是两条规则,per-rule 的
    比率只在各自桶里出现(把两个源的 revenue_drop 拼成一条"历史"会
    造出一段从未存在过的历史)。明细原样带在 ``reports`` 里。
    """
    items = [r for r in (reports or []) if isinstance(r, dict)]
    buckets = [b for r in items for b in (r.get("buckets") or [])
               if isinstance(b, dict)]
    return {
        "generated_at": str(generated_at or ""),
        "datasources": len(items),
        "summary": rollup(buckets),
        "reports": items,
    }


def _pct(v: Any) -> str:
    """比率 → 百分比文本;None(算不出/不足)=「—」,不折成 0%。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:.1%}"
    return "—"


def _insufficient_text(reasons: Iterable[Any], lang: str) -> str:
    words = _INSUFFICIENT_WORDS.get(lang, _INSUFFICIENT_WORDS["zh"])
    out = [words.get(str(r), str(r)) for r in (reasons or [])]
    return "、".join(out) if lang == "zh" else ", ".join(out)


def render_markdown(
    report: dict[str, Any], *, lang: str = "zh",
) -> str:
    """报告 → markdown(纯呈现;措辞随 lang,数字一律来自报告本身)。"""
    lang = "en" if str(lang).lower().startswith("en") else "zh"
    zh = lang == "zh"

    def L(zh_text: str, en_text: str) -> str:
        return zh_text if zh else en_text

    if not isinstance(report, dict):
        return ""
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    eff = summary.get("effects") if isinstance(summary.get("effects"), dict) else {}
    buckets = [b for b in (report.get("buckets") or []) if isinstance(b, dict)]

    lines: list[str] = []
    title = L("判定质量报告", "Decision quality report")
    ds = str(report.get("datasource") or "")
    lines.append(f"## {title}" + (f" — {ds}" if ds else ""))
    if report.get("generated_at"):
        lines.append("")
        lines.append(f"{L('生成时间', 'Generated at')}: {report['generated_at']}")

    lines.append("")
    lines.append(L(
        f"- 判定 {int(summary.get('total') or 0)} 次"
        f"(正常 {int(summary.get('ok') or 0)} / 告警 {int(summary.get('alert') or 0)}"
        f" / 错误 {int(summary.get('error') or 0)}),"
        f"触发率 {_pct(summary.get('triggered_rate'))}",
        f"- {int(summary.get('total') or 0)} verdict(s)"
        f" ({int(summary.get('ok') or 0)} ok / {int(summary.get('alert') or 0)} alert"
        f" / {int(summary.get('error') or 0)} error),"
        f" triggered {_pct(summary.get('triggered_rate'))}",
    ))
    lines.append(L(
        f"- 效果测量 {int(eff.get('measured') or 0)} 条"
        f"(有效 {int(eff.get('effective') or 0)} / 无变化 {int(eff.get('no_effect') or 0)}"
        f" / 判不了 {int(eff.get('unverifiable') or 0)} / 测量错误 {int(eff.get('errors') or 0)}),"
        f"有效率 {_pct(summary.get('effective_rate'))}",
        f"- {int(eff.get('measured') or 0)} measurement(s)"
        f" ({int(eff.get('effective') or 0)} effective / {int(eff.get('no_effect') or 0)} no-effect"
        f" / {int(eff.get('unverifiable') or 0)} unverifiable / {int(eff.get('errors') or 0)} errors),"
        f" effective rate {_pct(summary.get('effective_rate'))}",
    ))

    if buckets:
        lines.append("")
        lines.append(
            f"| {L('规则@版本', 'Rule@rev')} | {L('判定', 'Verdicts')} | "
            f"{L('告警', 'Alerts')} | {L('错误', 'Errors')} | "
            f"{L('触发率', 'Triggered')} | "
            f"{L('有效/已判', 'Effective/decided')} | "
            f"{L('有效率', 'Effective rate')} | {L('备注', 'Note')} |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for b in buckets:
            note = _insufficient_text(b.get("insufficient") or [], lang)
            b_eff = b.get("effects") if isinstance(b.get("effects"), dict) else {}
            lines.append(
                f"| {b.get('key', '')} "
                f"| {int(b.get('total') or 0)} | {int(b.get('alert') or 0)} "
                f"| {int(b.get('error') or 0)} | {_pct(b.get('triggered_rate'))} "
                f"| {int(b_eff.get('effective') or 0)}/{int(b.get('decided') or 0)} "
                f"| {_pct(b.get('effective_rate'))} | {note or '—'} |")
    else:
        lines.append("")
        lines.append(L("(尚无判定记录)", "(no verdicts yet)"))

    if any(str(b.get("rule_rev") or "") == REV_UNKNOWN for b in buckets):
        lines.append("")
        lines.append(L(
            f"注: ``{REV_UNKNOWN}`` = 记录产生于规则版本信息引入之前,"
            "无法归属到某个版本,单独成桶。",
            f"Note: ``{REV_UNKNOWN}`` = recorded before rule revisions existed;"
            " kept as its own bucket rather than guessed into one.",
        ))
    return "\n".join(lines)
