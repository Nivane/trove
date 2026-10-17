"""生成链归因的适配器面(唯一 IO 处):run 日志 / traces.jsonl / 结构化 records。

解析规则与保真度语义见设计 §1 与 §4;纯核心在 trove/eval/chain.py。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

from trove.eval.chain import (
    PlanFacts, Trajectory, plan_facts_from_json, plan_facts_text,
)

_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*) = (.*)$")
_NODE_RE = re.compile(r"^\[(\d+)\] ([a-z0-9_]+)")
_TRUNC_RE = re.compile(r"…\(\d+ (?:chars|items)\)\s*$")
_MARKERS = ("│", "├", "└", "═")
#: 消费的键 → Trajectory 字段名(适配器只认这些;其余原样留在 values 里供诊断)。
#: ``available`` 用**字段**词汇,不是日志键名:Task 4 的 ``loop_state`` 与
#: Task 7 的 ``merge_trajectories`` 都按字段名查它,别名键在这里归并(R19)。
_CONSUMED: tuple[tuple[str, str], ...] = (
    ("matched_tables", "matched_tables"), ("plan", "plan"),
    ("plan_json", "plan"), ("validation_hits", "validation_hits"),
    ("error_analysis", "error_analysis"), ("rollback_target", "rollback_target"),
    ("last_rollback_target", "rollback_target"), ("retry_count", "retry_count"),
)


@dataclass(frozen=True)
class RunLogFacts:
    """一个 run 日志的原始事实:每键最后一次非空取值(多行值已拼回)。"""

    question: str
    values: dict[str, str]
    seen: frozenset[str]
    nodes: tuple[str, ...]


def parse_run_log(text: str) -> RunLogFacts:
    """逐行扫描器:前缀行要么是 key=value(新键),要么终止连行;无前缀行是连行。"""
    question = ""
    m = re.search(r"^question: (.*)$", text, re.M)
    if m:
        question = m.group(1).strip()
    values: dict[str, str] = {}
    seen: set[str] = set()
    nodes: list[str] = []
    last_key: str | None = None
    for raw in text.splitlines():
        if raw[:1] in _MARKERS:
            last_key = None
            body = raw.lstrip("│├└─═ ")
            nm = _NODE_RE.match(body)
            if nm:
                nodes.append(nm.group(2))
            km = _KEY_RE.match(body)
            if km:
                key, val = km.group(1), km.group(2)
                seen.add(key)
                last_key = key
                if val != "" or key not in values:
                    if val != "":
                        values[key] = val
                    else:
                        values.setdefault(key, "")
        elif last_key is not None:
            values[last_key] = values.get(last_key, "") + "\n" + raw
    values = {k: v.rstrip() for k, v in values.items()}
    return RunLogFacts(question=question, values=values,
                       seen=frozenset(seen), nodes=tuple(nodes))


def _is_truncated(value: str) -> bool:
    return bool(_TRUNC_RE.search(value))


def _parse_table_list(raw: str) -> tuple[str, ...] | None:
    """JSON 列表或 Python repr 列表(单引号)→ 小写元组;解析不了 → None。"""
    if raw.strip() in ("", "None"):
        return None
    parsed = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    if not isinstance(parsed, (list, tuple)):
        return None
    return tuple(str(x).strip().lower() for x in parsed if str(x).strip())


def trajectory_from_log(facts: RunLogFacts, *, source: str = "logs") -> Trajectory:
    mt_state, mt = "absent", ()
    if "matched_tables" in facts.seen:
        parsed = _parse_table_list(facts.values.get("matched_tables", ""))
        if parsed is None:
            mt_state = "absent"
        elif parsed:
            mt_state, mt = "present", parsed
        else:
            mt_state = "empty"

    plan_text = facts.values.get("plan", "")
    plan_facts: PlanFacts | None = None
    pj_truncated = False
    pj_parsed: dict | None = None
    raw_pj = facts.values.get("plan_json", "").strip()
    if raw_pj and raw_pj != "None":
        try:
            pj = json.loads(raw_pj)
        except (json.JSONDecodeError, ValueError):
            pj = None
        if isinstance(pj, dict):
            pj_parsed = pj
            # 合法空字典 {} 是「计划为空」,不是截断;截断只会让 json.loads 失败。
            if pj:
                plan_facts = plan_facts_from_json(pj)
        elif raw_pj.startswith("{"):
            pj_truncated = True  # 600 字符静默截断:前缀不是合法 JSON
    plan_truncated = pj_truncated or _is_truncated(plan_text)
    if plan_facts is None and plan_text.strip():
        plan_facts = plan_facts_text(plan_text, truncated=_is_truncated(plan_text))
    # 状态由「键是否在场 + 是否读出内容」派生(M-4),不再只看 plan 键:
    # 只有合法 plan_json 的日志曾被打成 absent(fidelity 少计);被截断的
    # plan_json 是**观察到了内容**(前缀),不是「观察为空」。empty 仅当计划键
    # 在场且确实读不出任何内容。canonical 语料(plan 文本非空)不受影响。
    observed = "plan" in facts.seen or "plan_json" in facts.seen
    has_content = (plan_facts is not None or bool(plan_text.strip())
                   or pj_truncated or bool(pj_parsed))
    plan_state = ("present" if has_content
                  else ("empty" if observed else "absent"))

    retry: int | None = None
    raw_retry = facts.values.get("retry_count", "").strip()
    if raw_retry:
        try:
            retry = int(raw_retry)
        except ValueError:
            retry = None

    rollback = (facts.values.get("last_rollback_target", "").strip()
                or facts.values.get("rollback_target", "").strip())

    available = frozenset(field for key, field in _CONSUMED if key in facts.seen)
    return Trajectory(
        question=facts.question, source=source,
        matched_tables=mt, matched_tables_state=mt_state,
        plan_facts=plan_facts, plan_text_state=plan_state,
        plan_truncated=plan_truncated,
        validation_hits_raw=facts.values.get("validation_hits", ""),
        error_analysis=facts.values.get("error_analysis", ""),
        rollback_target=rollback, retry_count=retry,
        available=available,
    )


def load_run_logs(runs_dir: str | Path) -> dict[str, Trajectory]:
    """目录级加载:同题多日志按 mtime 后者胜(重跑即替换,与 runlog 同语义)。"""
    out: dict[str, Trajectory] = {}
    paths = sorted(Path(runs_dir).glob("*.log"),
                   key=lambda p: (p.stat().st_mtime, p.name))
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        facts = parse_run_log(text)
        if not facts.question:
            continue
        out[facts.question] = trajectory_from_log(facts)
    return out


def _trajectory_from_traces_run(run: dict) -> Trajectory:
    steps: dict[str, dict] = run.get("steps") or {}
    sl = steps.get("schema_linking") or {}
    mt_state, mt = "absent", ()
    if "matched_tables" in sl:
        raw_mt = sl.get("matched_tables")
        # 空列表 [] 也是「观察为空」,不能被 ``or ""`` 洗成 absent(m1)。
        parsed = _parse_table_list(
            str(raw_mt) if isinstance(raw_mt, (list, tuple))
            else str(raw_mt or ""))
        if parsed is None:
            mt_state = "absent"
        elif parsed:
            mt_state, mt = "present", parsed
        else:
            mt_state = "empty"

    planner = steps.get("planner") or {}
    plan_text = str(planner.get("plan") or "")
    plan_facts: PlanFacts | None = None
    pj = planner.get("plan_json")
    if isinstance(pj, dict) and pj:
        plan_facts = plan_facts_from_json(pj)
    elif plan_text.strip():
        plan_facts = plan_facts_text(plan_text, truncated=_is_truncated(plan_text))
    # M-4:planner 步存在但**没有计划键**时曾被打成 empty(声称「观察到计划为空」),
    # 还会在合并时遮蔽低优先级源的真实计划。状态只看计划键在不在 + 读出内容:
    # 键在但值为空 {} 才是 empty;键不在 = 这个源对 plan 没有观察(absent)。
    observed = "plan" in planner or "plan_json" in planner
    has_content = (plan_facts is not None or bool(plan_text.strip())
                   or pj not in (None, {}, ""))
    plan_state = ("present" if has_content
                  else ("empty" if observed else "absent"))

    aerr = steps.get("analyze_error") or {}
    gen = steps.get("gen_sql") or {}
    val = steps.get("validate") or {}
    hits = str(val.get("reason") or "")     # R31:设计 §3.1 保真表把 traces 的
    # `validation_hits` 记在 `validate.detail` 上;读了它,`available` 才是这套源
    # 对「链路自知过」这件事的诚实回答(否则只有 validate 一步自知的 run 会被判
    # undecidable —— 守卫与它自己的证据意见相反)。
    retry: int | None = None
    if isinstance(gen.get("retry"), int):
        retry = gen["retry"]
    elif isinstance(gen.get("attempts"), int) and gen["attempts"] > 0:
        retry = max(0, gen["attempts"] - 1)
    available = set()
    if mt_state != "absent" or "matched_tables" in sl:
        available.add("matched_tables")
    if plan_state != "absent":
        available.add("plan")
    # available 只认**实际在场的键**(m2):analyze_error 存在但没写 rollback 时
    # 不能声称「rollback_target 已被观察」—— 否则高优先级源的空值会遮蔽低
    # 优先级源的真实值,守卫又变成与自己的证据意见相反(R19 同款)。
    if "analysis" in aerr or "error" in aerr:
        available.add("error_analysis")
    if "rollback" in aerr:
        available.add("rollback_target")
    if val:
        available.add("validation_hits")
    if retry is not None:
        available.add("retry_count")
    return Trajectory(
        question=str(run.get("question") or "").strip(), source="traces",
        matched_tables=mt, matched_tables_state=mt_state,
        plan_facts=plan_facts, plan_text_state=plan_state,
        plan_truncated=_is_truncated(plan_text),
        validation_hits_raw=hits,
        # R39:analysis 缺席时 error 本身就是「链路跑过 analyze_error」的证据;
        # 只认 analysis 会把只带 error 的 run 判成 never_detected(与 R31 同款)。
        error_analysis=str(aerr.get("analysis") or aerr.get("error") or ""),
        rollback_target=str(aerr.get("rollback") or ""),
        retry_count=retry, available=frozenset(available),
    )


def load_traces(path: str | Path) -> dict[str, Trajectory]:
    """traces.jsonl → 按 question 的轨迹;同题多 run 取文件序最后一 run。"""
    runs: dict[str, dict] = {}
    order: list[str] = []
    p = Path(path)
    if not p.exists():
        return {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # traces 是尽力而为的旁路记录,坏行跳过(与 load_rows 的响亮失败刻意不同,注释写明)
        if not isinstance(e, dict):
            continue
        rid = str(e.get("run_id") or "")
        if not rid:
            continue
        kind = e.get("kind")
        if kind == "run":
            runs[rid] = {"question": str(e.get("question") or "").strip(),
                         "steps": {}}
            order.append(rid)
        elif kind == "step":
            run = runs.setdefault(rid, {"question": "", "steps": {}})
            # R41:非对象 detail 按坏值丢弃(旁路记录 best-effort;字符串 detail
            # 会在下游 ``sl.get`` 处把整份 traces 读取打断)。
            det = e.get("detail")
            node = str(e.get("node") or "")
            if isinstance(det, dict):
                run["steps"][node] = det
            else:
                # 坏值丢弃 ≠ 抹掉同一节点先前的完好 detail(m5)。
                run["steps"].setdefault(node, {})
        elif kind == "finish":
            runs.setdefault(rid, {"question": "", "steps": {}})
    out: dict[str, Trajectory] = {}
    for rid in order:
        run = runs[rid]
        if not run.get("question"):
            continue
        out[run["question"]] = _trajectory_from_traces_run(run)
    return out


def load_structured(path: str | Path) -> dict[str, Trajectory]:
    """eval_bird 新 records(含 plan/matched_tables 的 results.jsonl)→ 轨迹。

    坏行响亮失败(带行号),与 attribution.load_rows 同纪律:把截断文件读成
    「没问题」是诊断工具最不能有的失败模式。
    """
    out: dict[str, Trajectory] = {}
    p = Path(path)
    if not p.exists():
        return {}
    for lineno, line in enumerate(
            p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON: {e}") from e
        if not isinstance(d, dict):
            raise ValueError(f"{path}:{lineno} 不是 JSON 对象")
        q = str(d.get("question") or "").strip()
        # M-5:任一被消费字段在场就收 —— 只带 loop 证据(retries / validation_hits /
        # rollback_target)的记录曾整条被跳过,结构化源(最高优先级)的自知证据
        # 会因此丢失,把「检测过」判成 undecidable/never。
        if not q or not any(k in d for k in (
                "plan", "matched_tables", "retries", "validation_hits",
                "rollback_target")):
            continue
        mt_state, mt = "absent", ()
        if "matched_tables" in d:
            raw_mt = d.get("matched_tables")
            if isinstance(raw_mt, str):
                # R40:序列化成了 Python repr/JSON 字符串时按列表解析,绝不逐字符
                # 展开(``"['aa']"`` → ('[', "'", 'a', 'a', ...) 是静默污染桶)。
                parsed = _parse_table_list(raw_mt)
                if parsed is None:
                    mt_state = "absent"       # 读不出来 ≠ 观察为空(与 T6 同向)
                else:
                    mt_state = "present" if parsed else "empty"
                    mt = parsed
            elif raw_mt is None:
                pass                      # null = 无观察,不猜成「观察为空」
            else:
                try:
                    items = list(raw_mt)
                except TypeError:
                    raise ValueError(
                        f"{path}:{lineno} matched_tables 不是列表: {raw_mt!r}"
                    ) from None
                parsed = tuple(str(x).strip().lower() for x in items
                               if str(x).strip())
                mt_state = "present" if parsed else "empty"
                mt = parsed
        plan_facts = None
        pj = d.get("plan")
        if isinstance(pj, dict) and pj:
            plan_facts = plan_facts_from_json(pj)
        if plan_facts is not None:
            plan_state = "present"
        elif "plan" in d and pj == {}:
            plan_state = "empty"    # 空 dict = 观察到计划为空(M-4)
        else:
            plan_state = "absent"   # null / 缺键 = 没有观察,不猜
        retry = d.get("retries")
        retry = int(retry) if isinstance(retry, int) else None
        vh = d.get("validation_hits")
        available = set()
        if mt_state != "absent":
            available.add("matched_tables")
        if plan_state != "absent":
            available.add("plan")
        if vh is not None:
            available.add("validation_hits")
        if retry is not None:
            available.add("retry_count")
        if d.get("rollback_target"):
            available.add("rollback_target")
        out[q] = Trajectory(
            question=q, source="structured",
            matched_tables=mt, matched_tables_state=mt_state,
            plan_facts=plan_facts, plan_text_state=plan_state,
            # 已是字符串的 hits 原样保留:``"[]"`` 再 json.dumps 一次会变成非空
            # 字面量,loop_state 会把它读成「命中过」(m4)。
            validation_hits_raw=(
                vh if isinstance(vh, str) else json.dumps(vh, ensure_ascii=False)
            ) if vh else "",
            rollback_target=str(d.get("rollback_target") or ""),
            retry_count=retry, available=frozenset(available),
        )
    return out


def merge_trajectories(*maps: dict[str, Trajectory]) -> dict[str, Trajectory]:
    """逐字段合并:**先传 = 高优先级**(structured > traces > logs)。

    「取保真度最高者」= 高优先级源里该字段非 absent 就用它;没有任何源
    提供 → 保持 absent(判不了,不猜)。
    """
    questions: list[str] = []
    for m in maps:
        for q in m:
            if q not in questions:
                questions.append(q)
    out: dict[str, Trajectory] = {}
    for q in questions:
        cands = [m[q] for m in maps if q in m]

        def pick(field: str):
            for t in cands:
                if field == "matched_tables":
                    if t.matched_tables_state != "absent":
                        return t
                elif field == "plan":
                    # R38:状态非 absent 即该源对 plan 有话要说 —— 只看 facts 会把
                    # 「plan 空/被截断但无 facts」的单源合并洗成 absent,连带丢掉
                    # plan_truncated(R20 的覆盖计数)。
                    if t.plan_text_state != "absent" or t.plan_facts is not None:
                        return t
                elif field in t.available:
                    return t
            return None

        mt_src = pick("matched_tables")
        plan_src = pick("plan")
        err_src = pick("error_analysis")
        rb_src = pick("rollback_target")
        rc_src = pick("retry_count")
        vh_src = pick("validation_hits")
        available: set[str] = set()
        for t in cands:
            available |= set(t.available)
        out[q] = Trajectory(
            question=q,
            source="+".join(dict.fromkeys(t.source for t in cands if t.source)),
            matched_tables=mt_src.matched_tables if mt_src else (),
            matched_tables_state=mt_src.matched_tables_state if mt_src else "absent",
            plan_facts=plan_src.plan_facts if plan_src else None,
            plan_text_state=plan_src.plan_text_state if plan_src else "absent",
            plan_truncated=plan_src.plan_truncated if plan_src else False,
            validation_hits_raw=vh_src.validation_hits_raw if vh_src else "",
            error_analysis=err_src.error_analysis if err_src else "",
            rollback_target=rb_src.rollback_target if rb_src else "",
            retry_count=rc_src.retry_count if rc_src else None,
            available=frozenset(available),
        )
    return out
