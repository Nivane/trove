"""错误呈现层 — 把内部失败翻译成用户能读懂、能行动的一段话。

工作流内部的 ``state.error`` 是**排查语言**:节点名(``schema_linking``)、
规则号(``F1_shape``)、状态机术语(``优雅降级`` / ``无档可升``)。它适合进
日志与 trace,不适合直接摆在提问者面前 — 后者看到的是「回退目标
schema_linking 连续失败且无档可升,优雅降级」,既不知道出了什么事,也不
知道自己该做什么。

``present_error`` 补上这一层,产出固定四件套:

  kind        机器可读的归一类目(前端据此选图标/配色,不解析文案)
  title       一句话说清「没能完成」
  explanation 用人话说清发生了什么(不含节点名/规则号)
  suggestion  下一步动作(重试 / 换个问法 / 找管理员)

原始文本一字不改地留在 ``detail.raw`` — 管理层与日志仍可归因,不丢信息。
呈现层的判定**不另立一套**:分类复用 ``classify_error``,只在它覆盖不到的
「预算耗尽后放弃」语义上补一条确定性模式(降级文案本身即信号)。
"""

from __future__ import annotations

import re
from typing import Any

from trove.services.errors.classify import classify_error

# ── 降级信号(确定性,零 LLM)──────────────────────────────
# analyze_error 放弃迭代时会写这两类文案;它们是「试过了、停下了」的
# 唯一可靠信号,classify_error 的词典覆盖不到(它认的是外部故障形状)。
_GAVE_UP_PATTERNS = (
    re.compile(r"优雅降级"),
    re.compile(r"无进展"),
    re.compile(r"无档可升"),
    re.compile(r"budget exhausted", re.I),
    re.compile(r"degrad(e|ing) gracefully", re.I),
)

# 「试了几轮」——从原文里捞真实轮数,别对用户说假话
_ROUNDS_RE = re.compile(r"(?:连续\s*)?(\d+)\s*(?:轮|rounds?)", re.I)

# 失败节点归属:调用方没显式给 node 时,从原始文案里认(降级文案自带
# 「回退目标 <node>」)。缺省 "workflow" — 管理端据此知道去哪看日志。
_NODE_NAMES = (
    "route_intent", "parse_date", "schema_linking", "query_sketch",
    "gen_sql", "execute_sql", "validate", "reflect", "analyze_error",
    "metadata_check", "answer_metadata", "semantics", "insights",
    "conclusion", "chart", "hitl", "fast_match",
)


def _infer_node(text: str) -> str:
    for name in _NODE_NAMES:
        if name in text:
            return name
    return "workflow"


def _rounds(text: str) -> str:
    m = _ROUNDS_RE.search(text or "")
    return m.group(1) if m else ""


def _gave_up(text: str, lang: str) -> dict[str, str]:
    n = _rounds(text)
    if lang == "zh":
        tried = f"系统自动修正了 {n} 轮," if n else "系统自动修正了几轮,"
        return {
            "title": "这次没能给出可靠结果",
            "explanation": (
                f"你的问题需要的计算方式,{tried}仍然没能算稳,"
                "为避免给出错误的数字,已停止继续尝试。"
            ),
            "suggestion": "换个说法再问一次,或把问题拆小一点(比如先限定时间范围或地区)。",
        }
    tried = f"after {n} automatic repair attempts," if n else "after several automatic repair attempts,"
    return {
        "title": "No reliable answer this time",
        "explanation": (
            f"The computation your question needs did not stabilise {tried} "
            "so the run stopped rather than report a number it could not stand behind."
        ),
        "suggestion": "Try rephrasing, or narrow the question (e.g. fix a time range or a region first).",
    }


# ── 按错误类别的文案 ─────────────────────────────────────
# key = classify_error 的 domain;permission 由具体 class id 提到前面判,
# 因为「没权限」和「数据源拒绝」给用户的下一步动作完全不同。
_DOMAIN_COPY: dict[str, dict[str, dict[str, str]]] = {
    "ds": {
        "zh": {
            "title": "数据源暂时不可用",
            "explanation": "连接查询所用的数据源时失败了,这次没能取到数据。",
            "suggestion": "稍后重试;若持续失败,请联系管理员检查数据源连接。",
        },
        "en": {
            "title": "Datasource unavailable",
            "explanation": "The run could not reach the datasource, so no data was retrieved.",
            "suggestion": "Retry in a moment; if it keeps failing, ask an admin to check the connection.",
        },
    },
    "llm": {
        "zh": {
            "title": "模型服务暂时不可用",
            "explanation": "生成查询所用的模型服务没有正常响应,这次分析没能完成。",
            "suggestion": "稍后重试;若持续失败,请联系管理员检查模型配置。",
        },
        "en": {
            "title": "Model service unavailable",
            "explanation": "The model that writes the query did not respond, so the analysis stopped.",
            "suggestion": "Retry shortly; if it persists, ask an admin to check the model configuration.",
        },
    },
    "sql": {
        "zh": {
            "title": "查询没能跑通",
            "explanation": "生成的查询在数据源上执行失败,自动修正后仍未成功。",
            "suggestion": "换个问法再试;若反复出现,请联系管理员。",
        },
        "en": {
            "title": "The query did not run",
            "explanation": "The generated query failed against the datasource and automatic repair did not recover it.",
            "suggestion": "Try rephrasing; if it repeats, contact an admin.",
        },
    },
    "budget": {
        "zh": {
            "title": "问题太复杂,这次没算完",
            "explanation": "这个问题需要多轮分析,已超出单次可用的处理预算。",
            "suggestion": "把问题拆成几步来问(先看总览,再下钻到具体维度)。",
        },
        "en": {
            "title": "Too complex to finish in one run",
            "explanation": "The question needs more analysis rounds than a single run allows.",
            "suggestion": "Split it into steps — an overview first, then drill into the dimension you need.",
        },
    },
    "plan": {
        "zh": {
            "title": "这次没能稳定理解问题",
            "explanation": "系统对问题意图的把握不够稳定,没有给出可靠结果。",
            "suggestion": "把问题说得更具体一些(指明时间范围、地区或指标口径)。",
        },
        "en": {
            "title": "The question was not understood reliably",
            "explanation": "The intent could not be pinned down well enough to produce a trustworthy result.",
            "suggestion": "Be more specific about the time range, region, or metric definition.",
        },
    },
    "result": {
        "zh": {
            "title": "结果不符合预期",
            "explanation": "查询返回的数据形状与问题预期不符,已停止输出以免误导。",
            "suggestion": "换个问法再试;若反复出现,请联系管理员。",
        },
        "en": {
            "title": "The result did not match the question",
            "explanation": "The returned shape did not match what the question asks for, so nothing was presented.",
            "suggestion": "Try rephrasing; if it repeats, contact an admin.",
        },
    },
}

_PERMISSION = {
    "zh": {
        "title": "当前账号没有这项权限",
        "explanation": "这次请求涉及当前账号无权执行的操作,已被拒绝。",
        "suggestion": "如需该权限,请联系管理员开通。",
    },
    "en": {
        "title": "Your account lacks this permission",
        "explanation": "The request needs an operation this account is not allowed to perform.",
        "suggestion": "Ask an admin to grant the permission if you need it.",
    },
}

_GENERIC = {
    "zh": {
        "title": "这次没能完成",
        "explanation": "处理你的问题时发生了未预期的失败,分析已中断。",
        "suggestion": "重试一次;若仍失败,换个问法或联系管理员。",
    },
    "en": {
        "title": "The run did not complete",
        "explanation": "An unexpected failure interrupted the analysis.",
        "suggestion": "Retry; if it keeps failing, rephrase the question or contact an admin.",
    },
}

# class id → kind(前端选图标/配色;缺省由 domain 推导)
_ID_KIND = {
    "DS_AUTH": "permission",
    "SQL_PERMISSION": "permission",
    "BUDGET": "too_complex",
    "PLAN_DRIFT": "unclear",
    "INTENT_MISROUTE": "unclear",
}
_DOMAIN_KIND = {
    "ds": "datasource",
    "llm": "model",
    "sql": "query",
    "budget": "too_complex",
    "plan": "unclear",
    "result": "mismatch",
}

# 已放弃的轮次里,「重试原问题」仍有意义(换个采样可能就过了),故可重试;
# 权限/认证类则不该诱导用户反复点重试。
_NON_RETRYABLE_KINDS = {"permission"}


def present_error(
    text: str, lang: str = "zh", *, node: str = "", **extra: Any
) -> dict[str, Any]:
    """内部错误文本 → 用户可见四件套 + 机器细节。

    ``node`` 是失败节点(调用方已知时传入,比从文案里猜可靠)。
    额外关键字参数并入 ``detail``,供管理端展示上下文。
    """
    raw = text or ""
    verdict = classify_error(raw, context="workflow")
    cls = verdict.cls

    gave_up = any(p.search(raw) for p in _GAVE_UP_PATTERNS)
    if gave_up:
        kind = "too_complex" if "budget" in cls.id.lower() else "gave_up"
        copy = _gave_up(raw, lang)
    else:
        kind = _ID_KIND.get(cls.id) or _DOMAIN_KIND.get(cls.domain, "unknown")
        if kind == "permission":
            copy = _PERMISSION
        else:
            copy = _DOMAIN_COPY.get(cls.domain, _GENERIC)
        copy = copy.get(lang, copy["zh"])

    return {
        "kind": kind,
        "title": copy["title"],
        "explanation": copy["explanation"],
        "suggestion": copy["suggestion"],
        "retryable": kind not in _NON_RETRYABLE_KINDS,
        "detail": {
            "raw": raw,
            "node": node or _infer_node(raw),
            "error_class": cls.id,
            "domain": cls.domain,
            **extra,
        },
    }
