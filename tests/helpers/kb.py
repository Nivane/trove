"""KB 测试助手:经生产 writer 生成 OSSIE 格式的 semantics.yml 文本。

fixture 一律走 terms_to_ossie_document 产出——测试 YAML 不会与写端漂移。
"""
import yaml

from trove.services.kb.ossie_format import terms_to_ossie_document


def ossie_semantics_yaml(
    metrics: list[dict], model_name: str = "kb",
) -> str:
    """flat term dict 列表 → OSSIE semantic_model YAML 文本。

    Args:
        metrics: 与 /kb learn / TermCreate 同构的 flat 条目
            (term/aliases/mapping/tables/definition)。
    """
    doc = terms_to_ossie_document(metrics, model_name=model_name)
    return yaml.safe_dump(
        doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
