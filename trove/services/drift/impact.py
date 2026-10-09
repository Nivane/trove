"""变更影响面 —— ``touched_subjects`` → 受影响产物（纯函数、零 LLM）。

能力 1 P2 ``ImpactResolver`` 的最小版（``drift/service.py`` 的 with_impact
注释明言其未实现，本模块即它）。逐类扫描并标注**依据强度**（设计 §5.6）：

- ``reference``（结构化）：``decisions.yml`` 规则 subject 引用的 metric /
  维度 / 过滤字段；语义主题声明的 datasets / metrics。
- ``mention``（词面，best-effort）：``examples.yml`` 的 question/sql/tags 与
  ``lessons.yml`` 正文的规范化名字匹配。

读取失败**不吞**（坏 decisions.yml 一律抛给调用方折 ``degraded``）——「读
不到影响面」与「没有影响面」是两件事（I8 同款口径）。缺文件 = 空。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from trove.services.drift.models import ImpactSet, normalize_subject

_SEP_RE = re.compile(r"[`\"\[\]]")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _split(subject: str) -> tuple[str, str]:
    kind, _, name = str(subject).partition(":")
    return kind.strip().lower(), name.strip()


def _norm_text(text: str) -> str:
    return _SEP_RE.sub("", str(text or "").lower())


def _mentions(text: str, name: str) -> bool:
    """词面命中：全名归一后包含,或名字末段（>=3 字符）作为词元出现。

    best-effort 的边界写在这里：``loan.region`` 在 SQL 里常写作 ``region``，
    只比全名会漏；但末段匹配对短段（``id``）噪声太大，所以加长度门。
    """
    t = _norm_text(text)
    full = normalize_subject(name)
    if full and full in t:
        return True
    last = full.rsplit(".", 1)[-1]
    return len(last) >= 3 and re.search(rf"(?<![a-z0-9_]){re.escape(last)}(?![a-z0-9_])", t) is not None


def resolve_impact(kb_dir: Path, datasource: str, subjects: set[str]) -> ImpactSet:
    kb_dir = Path(kb_dir)
    ds_dir = kb_dir / datasource
    wanted = {_split(s) for s in subjects}
    wanted.discard(("", ""))
    metric_names = {n for k, n in wanted if k == "metric"}
    field_names = {n for k, n in wanted if k in ("field", "dataset")}
    dataset_names = {n for k, n in wanted if k == "dataset"}

    metrics: list[str] = []
    rules: list[str] = []
    topics: list[str] = []
    examples: list[str] = []
    lessons: list[str] = []
    basis: dict[str, str] = {}

    # ── reference：决策规则 subject 的 metric/维度/过滤字段引用 ──
    decisions = _load_yaml(ds_dir / "decisions.yml")
    if decisions:
        from trove.services.decision.rules import parse_document

        doc = parse_document(decisions)  # 坏文件抛给调用方（I8 同款）
        for rule in doc.rules:
            refs: list[tuple[str, str]] = []
            refs += [("metric", m) for m in rule.subject.metrics]
            refs += [("field", d) for d in rule.subject.dimensions]
            refs += [("field", str(f.get("field") or "")) for f in rule.subject.filters]
            tg = rule.subject.time_grain or {}
            if tg.get("field"):
                refs.append(("field", str(tg["field"])))
            hit = False
            for kind, ref in refs:
                norm = normalize_subject(ref)
                if kind == "metric" and norm in {normalize_subject(m) for m in metric_names}:
                    hit = True
                if kind == "field" and norm and (
                        norm in {normalize_subject(f) for f in field_names}
                        or norm.rsplit(".", 1)[0] in {normalize_subject(f).rsplit(".", 1)[0] for f in field_names if "." in f}):
                    hit = True
            if hit:
                rules.append(rule.id)
                basis[rule.id] = "reference"
                for m in rule.subject.metrics:
                    if m not in metrics:
                        metrics.append(m)
                        basis.setdefault(m, "reference")

    # ── reference：语义主题的 dataset/metric 声明 ──
    semantics = _load_yaml(ds_dir / "semantics.yml")
    for model in (semantics.get("semantic_model") or []):
        for t in (model.get("topics") or []):
            t_name = str(t.get("name") or "")
            t_ds = {normalize_subject(x) for x in (t.get("datasets") or [])}
            t_m = {normalize_subject(x) for x in (t.get("metrics") or [])}
            hit = bool(t_ds & {normalize_subject(d) for d in dataset_names}) or \
                bool(t_m & {normalize_subject(m) for m in metric_names}) or \
                bool(t_ds & {normalize_subject(f).rsplit(".", 1)[0] for f in field_names})
            if t_name and hit:
                topics.append(t_name)
                basis[t_name] = "reference"

    # ── mention：examples.yml / lessons.yml 词面 ──
    display = [n for _, n in sorted(wanted)]
    for e in (_load_yaml(ds_dir / "examples.yml").get("examples") or []):
        text = " ".join([str(e.get("question") or ""), str(e.get("sql") or ""),
                         " ".join(str(t) for t in (e.get("tags") or []))])
        if any(_mentions(text, n) for n in display):
            label = str(e.get("question") or e.get("sql") or "")[:80]
            examples.append(label)
            basis[label] = "mention"
    for lesson in (_load_yaml(ds_dir / "lessons.yml").get("lessons") or []):
        text = " ".join([str(lesson.get("pattern") or ""), str(lesson.get("note") or ""),
                         str(lesson.get("sql_snippet") or "")])
        if any(_mentions(text, n) for n in display):
            label = str(lesson.get("pattern") or "")[:80]
            lessons.append(label)
            basis[label] = "mention"

    return ImpactSet(metrics=metrics, examples=examples, rules=rules,
                     lessons=lessons, topics=topics, basis=basis)
