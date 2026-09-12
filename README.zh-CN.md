<div align="center">

# Trove

**对话你的数据。它会回答——并在每一次提问中变聪明。**

*开源的、可治理的对话式数据智能体:自然语言进,验证过的答案出——以一份人工确认的语义模型作为唯一可答边界。*

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2700%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

<br>

<img src="docs/assets/hero-chat.png" width="880" alt="Trove 对话界面:一句自然语言提问,得到结论、图表,以及右侧 12 步的分析过程">

<sub><i>一次提问的完整链路——经过验证的 SQL、图表,以及每一步推理过程,全部可见。</i></sub>

</div>

---

## Trove 是什么

Trove 是一个**自学习型对话式数据智能体**:用自然语言提问,得到由真实 SQL 支撑的 Markdown 答案——执行前与执行后都被验证;当答案只能是猜测时,它会拒绝;每一次提问都会让它变得更好。

它的承诺不是「永远正确」,而是**「错了也绝不轻易认输」**。三根支柱支撑这个承诺:

- **语义层划定边界。** 一份人工确认的语义模型(`semantics.yml`,Apache OSSIE 语义模型)是**唯一可答范围**:业务方已声明的 dataset/metric/field/relationship,仅此而已。模型之外的提问会被拒绝,并附带一条一键扩展模型的路径——绝不靠猜测裸表来回答。
- **确定性护栏闭环。** 生成的 SQL 要过零 LLM 规则链(形态/过滤/取值/排序)、AST 防火墙与带 SQL 版本回归的反思循环。错误的答案会被诊断、回滚、纠正,修正本身也被记住。
- **学习沉淀为资产。** 每一次纠正都蒸馏成一条 lesson,每一条被确认的问答都成为 reference SQL,跨会话记忆能召回 episode 与用户偏好。自动学习的内容一律以 `pending` 状态落地,直到管理员确认——知识在增长,同时保持可审查、可 git 管理、属于你。

## 为什么语义优先?

裸 LLM agent 依据原始 DDL 写 SQL——对业务含义的错误自信是常态。名为 `A11` 的列、状态码 `A`,离开业务词典毫无意义;「平均贷款金额」也无法从 schema 推导。RAG 有帮助:词表、示例与教训能锚定生成。但 RAG 只负责**供弹药,不负责画边界**——检索漏掉时,它照样用貌似合理的猜测作答。

Trove 走语义层路线(Cortex Analyst 一派),并加了一个让答案持续流动的变体:

- **全覆盖 → 编译。** 查询计划被编译到已声明的 metric/field/relationship 上——SQL 是权威产物,不是建议稿。
- **部分覆盖 → 编译骨架,交给生成补齐。** 只缺词表、值或口径(软 MISS)时,join/过滤/分组被权威编译,生成 agent 只补未覆盖的部分——执行前还有骨架保真校验兜底。
- **结构性 MISS → 拒绝、扩展、重答。** 模型结构上无法覆盖(未声明表/join 二义/fan-out)时,Trove 拒绝——并草拟一份模型扩展供你一键确认。**拒绝本身就是建模信号**;覆盖率随使用增长。

人类真正在意的一切——哪个口径算数、哪些 join 合法、枚举值是什么意思——在 YAML 里声明一次,对每个问题强制执行;而不是每个问题都重新猜一遍。

## 横向对比

| | 裸 LLM agent | 仅 RAG 的 NL2SQL | 纯语义层 | **Trove** |
|---|:---:|:---:|:---:|:---:|
| 从自然语言写 SQL | ✅(常错) | ✅ | ❌ | ✅ 受治理 |
| 划定可答边界(语义模型) | ❌ | ❌ | ✅ | ✅ |
| 覆盖外拒绝而非猜测 | ❌ | ❌ | 部分 | ✅ + 一键模型扩展 |
| 部分覆盖仍可作答(软 MISS 骨架) | ❌ | ✅(靠猜) | ❌ | ✅ 编译骨架 + 生成补缺 |
| 执行前零 LLM 验证 | ❌ | ❌ | 部分 | ✅ 规则链 + AST 防火墙 + EXPLAIN 守卫 |
| 执行后自纠(反思 + 回归) | ❌ | ❌ | ❌ | ✅ 诊断回滚重试 + 版本比对 |
| 按数据源学习;自动内容需人审 | ❌ | 部分 | 部分 | ✅ KB + 记忆,`pending` 至确认 |
| 数据在哪都能跑(6+ 引擎,自托管) | ✅ | ✅ | 依厂商 | ✅ Apache-2.0 开源 |

## 架构

### 系统总览

```mermaid
flowchart TB
    subgraph clients["Clients"]
        UI["Web UI<br/>(Vue SPA)"]
        REPL["CLI / REPL"]
        MCP["MCP server<br/>tools + resources"]
        API["REST API /v1<br/>SSE streaming"]
    end

    subgraph core["Trove core"]
        wf["LangGraph pipeline<br/>intent → linking → sketch → gen_sql<br/>→ execute → reflect"]
        semantic["Semantic layer<br/>semantics.yml · compiler · guardrails"]
        kb["Knowledge base<br/>terms · examples · rules · lessons"]
        memory["Memory<br/>episodes · preferences · profiles"]
        llm["LLM gateway<br/>litellm · any provider"]
        admin["Admin & governance<br/>kb init · draft review · drift"]
    end

    subgraph store["Storage"]
        yaml_src["YAML source of truth<br/>(git-manageable)"]
        rt["Runtime retrieval<br/>FTS5 / pg_bm25 + pgvector"]
        state_store["Internal state<br/>sessions · audit · checkpoints<br/>PostgreSQL prod · SQLite fallback"]
    end

    subgraph sources["Data sources"]
        src_pg["PostgreSQL"]
        src_my["MySQL / Doris"]
        src_ch["ClickHouse"]
        src_dk["DuckDB / SQLite"]
    end

    UI & REPL & MCP & API --> wf
    wf <--> llm
    wf --> semantic & kb & memory
    semantic --> yaml_src
    kb --> yaml_src
    yaml_src --> rt
    rt --> kb
    wf -->|"read-only adapters"| sources
    semantic -->|"compile over"| sources
    admin --> yaml_src
    state_store --> wf
```

### 一次问答如何发生

```mermaid
flowchart TB
    Q["Question"] --> INTENT["route_intent"]
    INTENT -->|"query"| PD["parse_date<br/>time context"]
    INTENT -->|"metadata"| MD["answer_metadata<br/>+ metadata self-check"]
    PD --> LINK["schema_linking<br/>dataset anchoring<br/>renders semantic_context"]

    LINK --> GATE{"Semantic gate"}
    GATE -->|"no model"| REFUSE["refuse<br/>run /kb init first"]
    GATE -->|"zero match"| REFUSE
    GATE -->|"covered"| FM{"fast_match<br/>KB exact hit?"}

    FM -->|"hit"| SQL["deterministic template SQL"]
    FM -->|"miss"| SKETCH["query_sketch<br/>LLM plan → typed IR"]

    SKETCH --> COMP{"SemanticCompiler"}
    COMP -->|"full coverage"| SQL
    COMP -->|"soft miss (word / value / definition)"| SKEL["PartialCompile skeleton<br/>joins · filters · grouping authoritative"]
    COMP -->|"hard miss (structural)"| REFUSE

    SQL --> GEN["gen_sql agent<br/>validate_sql · probe · check_result"]
    SKEL --> GEN
    REFUSE -->|"draft confirmed → re-enter"| PD

    GEN --> EXEC["execute_sql<br/>AST firewall · read-only<br/>skeleton-fidelity check"]
    EXEC --> CHK{"Zero-LLM rule chain<br/>shape · filters · values · ordering"}
    CHK -->|"violation"| FIX["analyze_error<br/>diagnose & rollback"]
    FIX --> SKETCH
    CHK -->|"pass"| REFLECT["reflect adjudication<br/>+ SQL version regression"]
    REFLECT --> OUT["answer + charts + insights"]
```

### 如何学习(并保持受治理)

```mermaid
flowchart LR
    RUN["Every question round<br/>question · SQL · verdict · corrections"] --> OBS["observe"]
    OBS --> EP["episodes<br/>factual recall (cross-session)"]
    OBS --> DR["draft reference example<br/>(pending)"]
    OBS --> LS["draft lesson<br/>(pending)"]
    REFUSE["refuse / missed coverage"] --> XT["semantic extension draft<br/>metric · field · relationship"]
    OBS --> PF["preference hints<br/>(pending)"]

    DR & LS & XT & PF --> GATE{"Admin confirm"}
    GATE -->|"confirm"| KB["Knowledge base<br/>examples.yml · lessons.yml<br/>semantics.yml · rules.yml"]
    GATE -->|"reject"| DISCARD["discarded"]
    EP --> MEM["memory store<br/>episodes · user facts"]
    KB --> RETR["deterministic retrieval<br/>anchored to matched datasets"]
    MEM --> RETR
    RETR --> GEN["next generation"]

    SCHEMA["live schema"] --> DRIFT{"vs declared semantics"}
    DRIFT -->|"stale"| STALE["drift report → rebuild / re-init<br/>preserves reviewed assets"]
    STALE --> KB
```

<p align="center">
<img src="docs/assets/hero-semantic.png" width="880" alt="Trove 语义层管理端:44 个指标、8 个数据集、56 个字段已声明,以及 10 条待审批">
</p>

<sub><i>治理面——模型声明了什么,以及还有什么等待人工确认。</i></sub>

## 能力清单

- **语义优先边界 + 分级作答** — 覆盖内全量编译;软 MISS 编译骨架、agent 补缺;结构性 MISS 拒绝 + 一键模型扩展重答。
- **确定性自校验** — 零 LLM 规则链(形态/过滤/取值/排序)、AST 防火墙(只读白名单、拦截 DML)、可选 EXPLAIN 行数守卫、带 SQL 版本回归的反思循环。
- **快径与智能体双路径** — 简单问题走确定性 KB 模板快径;复杂问题升级到 agentic ReAct 循环(`validate_sql` / `probe_query` / `check_result` 工具,由模型自行判定完成);歧义问题多候选生成 + 投票。
- **按数据源自学习的知识库** — `/kb init` 起草 schema 注释 + 语义模型 + 确定性术语与模板;确认过的问答成为 reference SQL;纠正蒸馏进 Hint Bank;漂移检测在模型落后于 schema 时报警。
- **统一跨会话记忆** — episodic 召回、自动提取的用户偏好、per user × datasource 画像;自动内容一律 `pending` 至管理员确认。
- **治理即特性** — YAML 是唯一真源(git 可审、可 diff);每次执行的工具调用都进审计;可选 HITL 人工确认;管理端提供 KB init、草稿审批与漂移报告。
- **可审计的分析轨迹** — Web UI 展示推理全过程:schema-linking 匹配、编译决策、规则链结果、agent 工具调用与修复原因。
- **Web UI + REST API** — Vue SPA 对话(流式、表格、图表、HITL 对话框);一切在 `/v1` 之下,含声明式语义查询端点 `/v1/semantic/query`——直接把结构化计划编译到语义模型,不走对话管线。
- **MCP server** — 通过 stdio / SSE / streamable-http 把 NL→SQL 暴露为工具与资源,供 Claude Code 等 MCP 客户端使用。
- **多引擎、同一套模式** — SQLite / PostgreSQL / MySQL / Doris / ClickHouse / DuckDB 适配器 + 每数据源独立 KB;内部统一存储在 PG(生产)/ SQLite(测试回退);管理端 checkpoint 时间轴支持从任意节点续跑。
- **LLM 无关、双语统一** — litellm 网关(OpenAI / DeepSeek / Anthropic / 任意兼容端点),每节点模型分层(草稿用便宜模型、反思用强模型),统一 `zh` / `en` 交互。
- **可观测、可中断** — 真实依赖健康检查、Prometheus 指标、request-id 日志关联、可选 Langfuse;取消查询会中断数据源驱动本身,而不只是挂起的协程。

## 快速开始

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖
uv sync

# 进入内置 BIRD 金融 demo 的交互 REPL
uv run trove --datasource demo

# 单次 CLI(JSON 输出,问题经 stdin 传入)
echo "Which region has the highest average loan amount?" | uv run trove-cli --datasource demo --print
```

### 接入你自己的数据

1. **连接** — 一个数据库 URL(或内置 demo):`--datasource postgres://user:pass@host:5432/db`。
2. **建模** — 运行 `/kb init`:它会基于你的实时 schema 起草初始语义模型(datasets/fields/metrics)。没有模型就没有答案:Trove 会礼貌地拒绝,直到你完成建模——这正是边界在工作。
3. **提问** — 然后自然提问。当 Trove 拒绝时,确认它起草的模型扩展,即可一步扩展覆盖并重答。

服务端部署下,同一流程在管理端完成(数据源注册 → KB init → 草稿审批)。

### Docker

前端(nginx 托管 SPA + `/v1` 反代)与后端(纯 JSON API)是相互独立的镜像,可独立构建与重启:

```bash
docker compose up --build          # 构建并启动(后端 :8000,前端 :8080)
docker compose build frontend      # 只重建前端镜像
docker compose restart backend     # 只重启后端
docker compose down
```

访问 `http://localhost:8080/`(本地默认登录 admin / `admin123`;生产用 `TROVE_ADMIN_PASSWORD` 环境变量)。compose 栈默认 PostgreSQL,已预载 BIRD demo 数据。真实对话需要 LLM 凭证:取消 `docker-compose.yml` 中只读挂载 `~/.trove/conf` 的注释,或在容器内提供 API key。

## 接口

### Web UI 与 API

后端是纯 JSON API(一切路由在 `/v1` 下,含 SSE 流式对话);前端是独立构建的 Vue SPA。

```bash
uv run trove serve --datasource demo      # 后端(仅 API)
cd frontend && npm run dev                # 本地开发 → http://localhost:5173/
cd frontend && npm run build              # 生产构建 → frontend/dist/
```

运维端点(无鉴权、无敏感数据):

- `GET /v1/health` — 真实依赖检查:探测内部存储与每个已连接数据源(`SELECT 1`),报告 LLM 配置存在性(不发起计费探测)。服务中返回 `200` + `"status": "ok" | "degraded"`;内部存储不可达返回 `503`。
- `GET /v1/metrics` — Prometheus 文本格式:按路由/状态的 HTTP、按 provider/model 的 LLM 调用与 token、按数据源的 SQL 执行、in-flight 计数。
- `GET /v1/semantic/query` — 声明式语义查询 API:把结构化计划直接编译到语义模型,不经对话管线。
- 每个响应携带 `X-Request-ID`,并出现在该请求的每一行日志里。
- 客户端中止端到端取消:图任务被取消,适配器的驱动级中断被触发(sqlite3 / psycopg / MySQL `KILL QUERY` / duckdb)。

### MCP server

```bash
uv run trove mcp                                                            # stdio(默认)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

工具:`ask_data` · `list_datasources` · `kb_status`。资源(只读):`trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`。

### REPL 命令

| 分组 | 命令 |
|---|---|
| 会话 | `/help` `/exit` `/clear` `/compact` `/tasks` |
| 知识 | `/tables` `/schemas` `/kb …` `/init` `/facts` |
| 系统 | `/model [model]` `/datasource [name]` `/trace` |

### 数据源

| 数据源 | 连接方式 | 安装 |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` | 内置 |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| Doris | `doris://user:pass@host:9030/database` | `uv sync --extra doris` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` | `uv sync --extra duckdb` |

每个数据库在 `.trove/kb/<database>/` 下沉淀自己的知识库。新增数据源:实现 `DatabaseAdapter` 方法并在 `registry.py` 注册。

## 安全(只读执行)

Trove 内置 AST 防火墙(只读语句白名单、拦截 DML、阻断危险函数与元数据表访问)与可选 EXPLAIN 行数守卫。**应用层不是安全边界**——请始终用专用只读账号连接:

```sql
-- PostgreSQL(覆盖未来对象)
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL(按库授权,固定来源 IP)
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

同样建议:用列级授权或视图隐藏敏感列;设置 `statement_timeout` / `lock_timeout`(PG)或 `MAX_EXECUTION_TIME`(MySQL);在数据库层用 `LIMIT` / `LEAST()` 强制行数上限。每次执行工具调用都会落审计日志。

## 配置

优先级:CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`:

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm 模型串
  model_fast: deepseek/deepseek-chat   # 便宜档:草稿、语义、洞察
  language: zh                         # 交互语言:zh / en
  semantic_first: true                 # 语义模型是唯一可答边界
  # node_models:                       # 每节点模型覆盖(query_sketch / reflect / …)
  #   query_sketch: deepseek/deepseek-chat
  memory:
    enabled: true                      # 统一记忆子系统
```

API key 放项目根 `.env`(自动加载、已 gitignore)或导出环境变量(如 `DEEPSEEK_API_KEY`)。自定义 OpenAI 兼容端点配在 `agent.providers`;可选 Langfuse 追踪走 `agent.observability.tracing.enabled`。

## 评测

用自己的数据、自己的语义模型、自己的口径复现准确率——Trove 自带 BIRD 金融 demo(schema 与官方 BIRD 导出一致)与全反思管线的评测脚本:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

逐题判定落 `.trove/eval/results.jsonl`,失败在 `failures.jsonl`;用 `scripts/distill_lessons.py` 批量蒸馏失败为 lessons。这里不放任何现成数字——请对着你自己的问题、你自己的语义模型、你自己的 schema 来量。(全量测试:~2700 个,mocked LLM,零网络零 key。)

## 开发

```bash
uv run pytest                     # 全量(~2700 测试,零网络/零 key)
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest tests/services/kb/  # 知识库
uv run pytest -m "not slow"       # 跳过慢测试
```

代码布局:`trove/workflow/`(LangGraph 图、节点、确定性规则链)· `trove/services/`(数据源适配器、KB、语义层、记忆、SQL)· `trove/llm/`(litellm 网关、agent 循环)· `trove/storage/`(统一后端:会话、审计、checkpoint)· `trove/agent/`(会话编排)· `trove/cli/`(REPL 与命令)。

更深的架构说明在 `CLAUDE.md`;`serve` 运行中的 REST API 文档在 `/v1/docs`。

## FAQ

**这是 text-to-SQL 框架还是 BI 工具?** 都不完全是——Trove 是对话式数据智能体:你提问,它规划、编译、执行、验证,再用 Markdown + 图表 + 可审计轨迹回答。它不打算成为仪表盘搭建器。

**它为什么拒绝问题?** 因为语义模型就是可答边界——这正是产品本身。一次拒绝意味着「模型未覆盖这个问题」,而且它随身带着一份草拟好的扩展,确认后即可扩大覆盖并重答。悄悄对裸表瞎猜,正是这套架构要消灭的失败模式。

**RAG 扮演什么角色?** RAG 负责喂生成——词表、reference SQL、lessons、情景记忆,以匹配到的数据集为锚做确定性检索。它决定 Trove **怎么写**覆盖内的 SQL;从不决定**能不能答**。

**需要向量数据库吗?** 不需要。开箱即用内置 SQLite FTS5 镜像检索;PostgreSQL 下同一存储同时承载 pg_bm25 + pgvector 混合检索;情景记忆在配置了 embedder 时可用向量,未配置时优雅回退词法匹配。

**需要什么 LLM?** 任意 litellm 兼容模型。质量建议:强模型做反思/裁决,便宜模型做规划与洞察(`conf/agent.yml` 每节点分档)。注意:demo REPL 没有 LLM 凭证就不作答——不存在静默的「mock 答案」回退。

**schema 漂移了,语义模型怎么保持诚实?** 运行时守卫(编译 guardrail、表存在性校验)保证行为安全;主动漂移检查把声明与实时 schema 对比,在管理端报 `stale`——此时用 `/kb init` 重建会保留人工审过的定义。

## 参与贡献

欢迎 bug 报告、功能想法、文档与 pull request。请保持改动聚焦,提交前确保测试通过。活跃开发中的架构与设计文档随团队维护——较大的改动请先开 issue 讨论。

## License

Trove 以 [Apache License 2.0](LICENSE) 发布。
