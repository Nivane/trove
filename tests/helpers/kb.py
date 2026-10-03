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


def topic_model_yaml(datasets: list[str], topics: dict[str, list[str]]) -> str:
    """最小语义模型:只含 datasets 声明 + topics 段(主题域测试专用)。

    ``terms_to_ossie_document`` 不产 topics(主题域是人工声明段,不在
    term 写端的输出面上),所以这里手写 —— 但参数化到「声明集 × 域」这
    两个语义事实,而不是让各测试各拷一份完成度不同的 YAML。dataset 只带
    名字是 parse_ossie 接受的存量形态(字段缺省为空)。
    """
    doc = {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "kb",
            "datasets": [{"name": d} for d in datasets],
            "topics": [
                {"name": name, "datasets": list(ds)} for name, ds in topics.items()
            ],
        }],
    }
    return yaml.safe_dump(
        doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
