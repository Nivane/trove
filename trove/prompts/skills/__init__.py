"""Skill templates — node-triggered methodology blocks.

A skill is a reusable "how-to" prompt block bound to pipeline nodes via
trigger conditions in ``manifest.yml`` (same directory). Skills are matched
deterministically — by node name plus state features — rendered with the
regular prompt loader (``trove.prompts.loader.render``, bilingual
``.en/.zh`` with fallback), and appended to the node's system prompt.

Design boundary: skills carry cross-datasource methodology (how to plan,
how to diagnose failures). Facts about a datasource belong in the KB, not
here.

Public API:
    match_trigger(key, cond, value) -> bool   trigger 语义的**唯一**实现
    matched_skills(node, **ctx) -> list[str]   matching skill names
    render_skills(node, lang="en", **ctx) -> str   rendered blocks, joined
    fence_org_skill(name, body) -> AdminConfirmed   org 正文围栏 + 来源标注
    append_skill_block(system_text, block) -> str   追加技能块的**唯一**门

两条通道的区别（`render` vs `append`）
--------------------------------------
``render()`` 在**插值**上隔离参数：模板里 ``{{ var }}`` 的位置，参数过
``_isolate_vars``。但**技能正文是追加的**，不经过任何模板变量 —— 追加在结构上
长得不像插值，所以评审时没人想到 ``render()`` 管得到它，三个调用点因此各自写
f-string 拼接、全部裸奔。

所以追加要有自己的门：``append_skill_block``。它不隔离内容（技能是管理员确认过的
**指令**，隔离它等于删方法论），它把追加收敛到一个可被测试钉住的位置。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.llm.untrusted import AdminConfirmed
from trove.prompts.loader import render

_MANIFEST_PATH = Path(__file__).parent / "manifest.yml"
_cache: list[dict] | None = None


def _load_manifest() -> list[dict]:
    """Manifest entries, cached for the process lifetime."""
    global _cache
    if _cache is None:
        data = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8")) or []
        _cache = list(data)
    return _cache


def match_trigger(key: str, cond: object, value: object) -> bool:
    """一个 trigger 字段的匹配 —— **语义的唯一实现**。

    两条语义，按字段名分派：

    - ``role``：**列表对列表，有交集即命中**。``tool_roles`` 天然是列表
      （一个用户可以同时是 analyst 与 admin），标量相等语义下
      ``["analyst"] == ["analyst", "admin"]`` 恒假，配了 role 的 trigger
      会永远不命中。``value`` 缺失（``None`` / ``[]``，CLI 直用或未登录）
      → **不命中**：收窄条件在信息缺失时应当保守。
    - 其余：标量相等，或列表成员（OR）。

    收在唯一一处是因为 **code skill 与 org skill 用的是同一套 trigger 字段名**
    —— 两处各写一份，同名字段在两处语义不同，是最难查的一类分歧。
    """
    if key == "role":
        if isinstance(value, list):
            have = set(value)
        elif value:
            have = {value}
        else:
            return False
        want = set(cond if isinstance(cond, list) else [cond])
        return bool(have & want)
    if isinstance(cond, list):
        return value in cond
    return value == cond


def matched_skills(node: str, **ctx: object) -> list[str]:
    """Names of skills whose trigger conditions match node + ctx.

    A skill matches when its ``node`` trigger equals ``node`` and every
    other trigger field equals the corresponding ctx value. Skills without
    triggers never match.
    """
    out: list[str] = []
    for skill in _load_manifest():
        triggers = skill.get("triggers") or {}
        if not triggers or triggers.get("node") != node:
            continue
        if all(
            match_trigger(k, v, ctx.get(k))
            for k, v in triggers.items()
            if k != "node"
        ):
            out.append(skill["name"])
    return out


def render_skills(node: str, lang: str = "en", **ctx: object) -> str:
    """Render all matched skill blocks for ``node``, blank-line joined.

    Returns "" when nothing matches — safe to append unconditionally.
    """
    blocks = [
        render(f"skills/{name}/system", lang=lang)
        # lang 是具名形参,不在这里补回,它就到不了 matched_skills ——
        # 与 org 侧 _match_org 的 lang 是**同一个**死症:配了 lang 触发
        # 条件的技能永远不命中,而两处都看得出来"代码是对的"。
        for name in matched_skills(node, lang=lang, **ctx)
    ]
    return "\n\n".join(blocks)


def fence_org_skill(name: str, body: str) -> AdminConfirmed:
    """把一份 org skill 正文围栏 + 标注来源，返回**登记过的**配置文本。

    **只加边界，不删句子。** 处置是「标注来源」，不是「净化内容」：指令性文本的
    安全属性来自来源认证（管理员确认后落库），不来自模式扫描 —— 对方法论做模式
    扫描，实测一句「忽略之前的指令」就会让整份正文变成
    ``[data: content isolated]``，一句话毁掉一份方法论是误伤不是安全。

    围栏给两头用：模型知道这段是指令而非数据；事后审计能分辨「这条指令来自
    哪份配置」与「来自用户 / 来自库里的行」。

    返回 ``AdminConfirmed`` 而不是裸 ``str``：正文经 ``load_skill`` 工具回喂时
    要过隔离核，登记过的类型才不会被当数据作废（见 ``llm/untrusted.py``）。
    """
    # 名字来自技能目录，已被 ``_NAME_RE`` 限成 ``[a-z0-9-]``；这里仍然转义，
    # 因为围栏是信任边界的标记，属性值不该有任何注入余地。
    safe = str(name).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return AdminConfirmed(
        f'<org_skill name="{safe}" source="admin-confirmed">\n{body}\n</org_skill>'
    )


def append_skill_block(system_text: str, block: str) -> str:
    """把技能块追加进 system prompt —— **追加的唯一门**。

    三个节点（``gen_sql`` / ``analyze_error`` / ``query_sketch``）此前各写一遍
    ``f"{system_text}\\n\\n{block}"``。散落的 f-string 不会被任何清单枚举到
    （调用点没变、投递内容变了），所以收成具名函数：追加从此有唯一的、可钉的
    位置，新增节点不会漏接。

    空块原样返回 ``system_text``（不追加空行）—— 与 ``render_skills`` 返回 ``""``
    的约定配对，让调用方可以无条件调它。
    """
    return f"{system_text}\n\n{block}" if block else system_text
