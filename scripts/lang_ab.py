"""语言 A/B 实验:同一道 BIRD Q6 用三种表述跑 reflection 管线,对比 gold。

- en-原始: BIRD 英文原题(保留协调歧义)
- zh-直译: 中文直译(同样保留歧义)
- zh-消歧: 中文改写,把"周发放"放进 among 限定范围(与 gold 同义)

一次实验仅供观察,非统计结论(温度/重试都会引入方差)。
"""

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.core.config import ConfigLoader
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.kb.service import KbService
from trove.tracing.local import configure_trace_store
from trove.tracing.runlog import create_tracer
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.state import WorkflowState

GOLD_SQL = (
    "SELECT `T2`.`account_id` FROM `loan` AS `T1` "
    "INNER JOIN `account` AS `T2` ON `T1`.`account_id` = `T2`.`account_id` "
    "WHERE DATE_FORMAT(CAST(`T1`.`date` AS DATETIME), '%Y') = '1997' "
    "AND `T2`.`frequency` = 'POPLATEK TYDNE' ORDER BY `T1`.`amount` LIMIT 1"
)
EVIDENCE = "'POPLATEK TYDNE' stands for weekly issuance"

QUESTIONS = {
    "en-原始": "Among the accounts who have approved loan date in 1997, "
               "list out the accounts that have the lowest approved amount "
               "and choose weekly issuance statement.",
    "zh-直译": "在1997年批准了贷款的账户中，列出批准金额最低、且选择周发放报表的账户。",
    "zh-消歧": "在1997年批准了贷款、且选择周发放报表的账户中，找出批准金额最低的账户。",
}


def normalize_rows(rows: list[list]) -> list[tuple]:
    return sorted(tuple(str(v) for v in row) for row in rows)


async def main() -> None:
    config = ConfigLoader.load_agent_config("conf/agent.yml")
    configure_trace_store(Path.home() / ".trove")
    registry = ConnectorRegistry()
    adapter = await registry.register(
        parse_datasource_url("mysql://root:root@127.0.0.1:3306/financial"),
        set_default=True,
    )
    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=KbService(Path.cwd()),
    )
    graph = build_graphs(services)["reflection"]

    gold_rows = (await adapter.execute(GOLD_SQL)).rows
    print(f"gold 答案: {gold_rows}", flush=True)

    for label, question in QUESTIONS.items():
        print(f"\n════ {label}: {question}", flush=True)
        run_id = f"lang-ab-{int(time_import())}"
        tracer = create_tracer(run_id, verbose=False)
        state = WorkflowState(
            session_id=run_id, question=question, evidence=EVIDENCE,
            run_id=run_id, lang=config.language,
        )
        tracer.start_run({"question": question, "gold_sql": GOLD_SQL, "model": config.target})
        final = WorkflowState.model_validate(await graph.ainvoke(state))
        pred_rows = (await adapter.execute(final.sql)).rows if final.sql and not final.error else []
        matched = normalize_rows(pred_rows) == normalize_rows(gold_rows)
        print(f"verdict={final.verdict} · retry={final.retry_count} · "
              f"pred={pred_rows} · match={matched}", flush=True)
        print(f"sql: {final.sql}", flush=True)
        tracer.finish({"verdict": final.verdict, "retry_count": final.retry_count})

    await registry.close_all()


def time_import() -> int:
    import time
    return int(time.time())


if __name__ == "__main__":
    asyncio.run(main())
