"""业务主题域的选择与收敛(零 LLM,纯函数)。

主题域在语义模型里声明(``SemanticModel.topics``),这里只做三件事:

1. **解析选择**:调用方给的 topic 名 → 声明里的那一个(大小写/空白不敏感);
   解析不到 = 显式的失败信号(``TopicResolution``),**不静默退回全量** ——
   用户选的是范围,范围没了就该说,而不是把问题答在更大的数据上。
2. **算作用域**:``topic.datasets`` ∩ 模型当前声明的数据集。交集而不是直接
   采信声明:主题域写在 YAML 里、模型会随 ``kb init`` 演进,悬空名字只会
   让作用域比作者以为的更窄。交集为空也是显式状态(``empty_scope``)。
3. **过滤**:把任意名字序列收敛到作用域内(保序)。

作用域**只收敛锚定**(问题"关于哪些数据集"),不收敛 join 路径:编译器
按声明的 join 图补齐中间表是既有语义,主题域不是关系图的裁剪器。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from trove.services.semantic_layer.models import TopicDomain


@dataclass
class TopicResolution:
    """topic 名 → 声明主题域 + 生效作用域。

    status: ``"none"``(未选择,行为与不启用主题域一致) | ``"ok"`` |
        ``"not_found"``(名字不在模型里) | ``"empty_scope"``(名字在,
        但声明数据集已全部不在模型里 —— 域过期,不是"不限制")。
    """

    status: str = "none"
    requested: str = ""
    topic: TopicDomain | None = None
    scope: list[str] | None = None  # None = 不收敛;"ok" 时是生效作用域(非空)

    @property
    def active(self) -> bool:
        return self.status == "ok"

    @property
    def scope_set(self) -> set[str] | None:
        return set(self.scope) if self.scope is not None else None


def find_topic(model: Any, name: str) -> TopicDomain | None:
    """模型声明里按名找主题域(trim + 大小写不敏感;重名取第一条)。"""
    wanted = str(name or "").strip().lower()
    if not wanted or model is None:
        return None
    for t in getattr(model, "topics", None) or []:
        if str(t.name or "").strip().lower() == wanted:
            return t
    return None


def resolve_topic(model: Any, name: str) -> TopicResolution:
    """topic 名 → 解析结果(未选/命中/未找到/域过期四态)。"""
    requested = str(name or "").strip()
    if not requested:
        return TopicResolution(status="none")
    topic = find_topic(model, requested)
    if topic is None:
        return TopicResolution(status="not_found", requested=requested)
    declared = [str(d.name) for d in (getattr(model, "datasets", None) or [])]
    declared_set = set(declared)
    scope = [d for d in topic.datasets if d in declared_set]
    if not scope:
        return TopicResolution(
            status="empty_scope", requested=requested, topic=topic, scope=[])
    return TopicResolution(
        status="ok", requested=requested, topic=topic, scope=scope)


def in_scope(scope: set[str] | None, name: str) -> bool:
    """名字是否落在作用域内(``None`` = 未收敛,一律放行)。"""
    return scope is None or str(name) in scope


def filter_scoped(names: Iterable[str], scope: set[str] | None) -> list[str]:
    """名字序列收敛到作用域内(保序去重);``scope is None`` 原样返回。"""
    out: list[str] = []
    for n in names:
        s = str(n)
        if not in_scope(scope, s):
            continue
        if s not in out:
            out.append(s)
    return out


def topic_names(model: Any) -> list[str]:
    """模型声明的主题域名(按声明顺序)—— 拒绝文案/前端选择器的可选清单。"""
    return [str(t.name) for t in (getattr(model, "topics", None) or []) if t.name]


def render_topic_line(res: TopicResolution) -> str:
    """主题域在 semantic_context 里的一行声明(确定性;仅命中时渲染)。

    渲染给生成侧看的是**作用域事实**(本问收敛在哪些数据集),不是营销
    文案:生成据此不再往域外找表。
    """
    if not res.active or res.topic is None:
        return ""
    line = f"Topic domain: {res.topic.name}"
    if res.topic.description:
        line += f" — {' '.join(str(res.topic.description).split())}"
    line += f" (scope: {', '.join(res.scope or [])})"
    return line
