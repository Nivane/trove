# Trove

Trove — Talk to your data.
It answers, and learns with every question.

把数据库变成对话：自然语言提问，Trove 自动完成 schema 匹配、SQL 生成与自校验、执行与反思裁决，输出 Markdown 答案，答错自动诊断根因并回滚修正。

它边用边学——每次问答与修正沉淀为该数据源的专属知识（注释、术语、参考 SQL、规则、经验），冷启动靠规则兜底，越用越准。

## 功能特性

- **LangGraph 流水线**（`reflection` 工作流，全链路）：
  `route_intent → parse_date → schema_linking → planner → gen_sql → execute_sql → select → validate → reflect → (analyze_error 回滚) → output`
  - **意图路由**：LLM 判定 + 证据校验，分流两条路径——数据查询（query）与元数据问答（metadata：`answer_metadata → metadata_check` 自校验循环）
  - **gen_sql（agentic 默认）**：ReAct 循环，模型持 `validate_sql` 工具自校验、自行判定结束（≤6 轮）；异常或空产出自动回退经典「生成 → 校验重试」子图；KB 精确命中（词重叠 ≥0.95）时直接采用标准 SQL，跳过生成
  - **多候选共识**：备选候选以更高温度生成 → `select` 裁决（KB 命中时跳过）
  - **analyze_error 根因诊断**：LLM 判定失败根因，沿 `gen_sql → planner → schema_linking` 回滚阶梯重跑，防环守护；reflect 裁决与执行错误共享 ≤10 轮修正上限
  - 三个工作流：`reflection`（默认，带自校正）/ `fixed`（快速直通）/ `empty`（调试透传）
- **确定性时间解析**（parse_date）：相对时间表达（"最近7天" / "last week"）解析为绝对范围，未命中静默透传
- **上下文预算**：gen prompt 的可选块（示例/规则/术语/经验/计划/历史）按优先级装入 2500 token 预算，实际装载量进入可观测
- **优雅降级**：SQL 生成/执行失败时输出可读的错误说明，不中断会话
- **流式输出**：REPL 实时显示 thought / SQL / 结果 / 答案；Ctrl+C 可取消运行中的查询
- **双轨持久化**：会话消息（`~/.trove/sessions/`）+ 图状态检查点（`~/.trove/checkpoints.db`，支持时间旅行）
- **本地轨迹**：每次运行记录 span 树轨迹（节点耗时、每次 LLM 调用输入输出、工具调用）到 `~/.trove/traces.jsonl`，`/trace` 回放完整推理链路，零外部依赖
- **可演化知识库**（按数据源隔离，`.trove/kb/<datasource>/`）：
  - `schema_notes.yml` — 表/列注释、指标口径（`/kb init` 生成）
  - `semantics.yml` — 业务术语 → 物理映射（中文问题匹配、口径统一）
  - `examples.yml` — 参考 SQL + 模板（few-shot 注入 gen_sql）
  - `rules.yml` — 数据源规则（注入生成提示词）
  - `lessons.yml` — Hint Bank 经验库：从修正闭环与评测失败提炼的教训，按模式匹配注入，pending/confirmed 两级
  - 检索以 schema linking 的 `matched_tables` 为锚做确定性过滤，KB 精确命中短路
  - `/kb learn` 半自动演化：LLM 起草 → 人工确认 → 入库
- **多语言**：`language: en / zh` 统一交互语言——提示词、答案、轨迹全程使用所选语言（不按问题语言自动检测）
- **多模型**：litellm 网关，支持任意兼容 provider（OpenAI / DeepSeek / Anthropic / …），可配 `api_base` 接非官方端点；已适配推理模型（reasoning 输出占用 token 预算的处理）

## 快速开始

```bash
uv sync          # 安装依赖（含 dev 测试工具）
uv run pytest    # 全量测试（mock LLM，零网络零 key）

# 交互式 REPL（内置 BIRD 金融 demo 数据源）
uv run trove --datasource demo

# 一次性 JSON 输出（问题走 stdin）
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print
```

REPL 命令：

| 分组 | 命令 |
|---|---|
| 会话 | `/help` `/exit` `/clear` `/compact`（压缩历史） |
| 元数据 | `/tables` `/schemas` `/table_schema <表>` `/databases` `/kb …` `/trace` |
| 系统 | `/model [模型]` `/datasource [名]` `/init` |

CLI 参数：`--datasource/-d`（demo 或 `scheme://` URL）· `--config/-f` · `--model/-m` · `--print/-p`（JSON 输出）· `--workflow/-w`（reflection/fixed/empty）· `--version/-v`

## Web UI（trove serve）

```bash
uv run trove serve --datasource demo
# 打开浏览器 → http://127.0.0.1:8000/ （自动跳转 /ui/）
```

- 单页聊天界面（纯静态 HTML + vanilla JS，零构建），`GET /` 重定向到 `/ui/`；答案流式展示每步轨迹（意图/匹配表/计划/SQL/校验/反思，含耗时与重试轮次）
- 接口：`POST /v1/chat`（SSE 流式）、`GET/POST/DELETE /v1/sessions[/{id}]`、`GET /v1/catalog/*`、`GET/POST /v1/kb/*`；API 文档见 `/v1/docs`
- 会话 ID 与界面语言（zh/en）保存在浏览器 localStorage；对话历史由服务端按 session 持久化
- 停止按钮只中断客户端读取——服务端会跑完本次查询并持久化，刷新页面即可看到结果

## 配置

配置优先级（模型选择）：CLI `--model` > `conf/agent.yml` / `~/.trove/conf/agent.yml`。

```yaml
# conf/agent.yml 示例
agent:
  target: deepseek/deepseek-v4-flash    # litellm 模型字符串（推理模型如 deepseek-reasoner 亦可）
  language: en                          # 交互语言 en / zh：提示词、答案、轨迹统一使用
  date_parser: true                     # 确定性相对时间解析（未命中静默透传）
  explain_semantics: false              # 生成 SQL 后 LLM 说明语义（输出与 HITL 确认时展示）
  hitl: false                           # 执行前人工确认（LangGraph interrupt，需 persistence/checkpointer）
  insights: false                       # 执行后 LLM 基于结果生成洞察
  providers:
    - name: openai                  # 非官方端点（兼容 OpenAI API）示例
      litellm_params:
        api_key: ${OPENAI_API_KEY}   # 自动做环境变量替换
        api_base: https://your-endpoint/v1
```

- **API key**：写入项目根 `.env`（启动时自动加载，已 gitignore），或直接 export 环境变量（如 `DEEPSEEK_API_KEY`）
- `~/.trove/conf/agent.yml` 为全局用户级配置；`agent.yml` 里的 `providers[].litellm_params` 会按模型前缀透传给 litellm

**可观测性（Langfuse）**：

```yaml
# conf/agent.yml
agent:
  observability:
    tracing:
      enabled: true
```

```bash
# .env 提供 Langfuse 凭证
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com   # 或自托管地址
```

启用后每个 LLM 调用（意图判定 / 计划 / gen_sql 生成与修正 / 裁决 / 错误诊断）都会进入 Langfuse，prompt 与输出全程可见，并按 `session_id`、`node`、`question` 元数据分组——CoT 每一步可回溯。本地轨迹（`/trace`）始终可用，不依赖外部服务。

## 知识库使用

1. 启动 REPL 后 `/kb init` —— LLM 起草表/列中文注释（大 schema 分块起草后合并，解析失败自动修复一轮），可选：
   - `--docs <dir>` 导入官方列描述（权威，覆盖 LLM 草稿）
   - 低基数文本列自动探测 distinct 值，辅助 LLM 猜枚举含义
   - 业务术语与参考模板由确定性规则生成
2. 编辑 `.trove/kb/<数据源名>/` 下 YAML 补充口径、术语（term → SQL 表达式映射）
3. `/kb reload` 立即生效
4. 正常提问；满意的问答用 `/kb learn` → 审阅草稿 → `/kb learn --yes` 沉淀为参考 SQL
5. `/kb list` 查看各数据源知识条目数

**Hint Bank（经验库）**：修正闭环成功后，修正理由自动沉淀为待确认教训；`/kb lessons` 查看、`/kb lessons --yes` 确认入库。批量制造：`uv run python scripts/distill_lessons.py --datasource <名> [--confirm]` 从 eval 失败记录逐条提炼经验（按 pattern 去重、过滤管线噪声）。

知识库按数据源隔离：切换 `/datasource` 后检索自动切换；YAML 是唯一事实源（可 git 管理），SQLite 镜像仅供运行时检索。

## 数据源

| 数据源 | 连接方式 | 安装 |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` / `sqlite://:memory:` | 内置 |
| MySQL | `mysql://user:pass@host:3306/database`（端口可省略，默认 3306） | `uv sync --extra mysql` |
| ClickHouse | `clickhouse://user:pass@host:8123/database`（默认 8123） | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` / `duckdb://:memory:` | `uv sync --extra duckdb` |

- 数据源名 = **数据库名**（知识库目录 `.trove/kb/<数据库名>/` 与之对应，每个库各自演化）
- 驱动按需惰性导入：未安装对应 extra 时给出明确提示，不影响其他数据源
- 连接失败时回退到 demo 并输出警告
- 新增适配器：实现 `DatabaseAdapter` 五个抽象方法（`trove/services/datasource/adapters/base.py`），在 `registry.py` 的 `_ADAPTER_REGISTRY` 注册

**真实服务集成测试**（未设置环境变量时自动跳过，CI 零网络约束不变）：

```bash
MYSQL_TEST_URL="mysql://user:pass@host:3306/anydb" \
  uv run pytest tests/services/test_mysql_adapter.py -m integration

CLICKHOUSE_TEST_URL="clickhouse://default:pass@host:8123/default" \
  uv run pytest tests/services/test_clickhouse_adapter.py -m integration
```

（DuckDB 集成测试用内存库，无需外部服务，常开。）

## 评测

BIRD 开发集执行准确率（EX）评测，跑真实数据源 + 完整 reflection 管线，对比 gold SQL 执行结果：

```bash
# 单库全量 / 限题（答错题写入 .trove/eval/failures.jsonl，供 distill_lessons 提炼经验）
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

辅助脚本：`import_golden_examples.py`（gold SQL 导入 examples.yml）· `import_bird_descriptions.py`（官方 CSV 描述导入 schema_notes.yml）· `probe_enums.py`（枚举探测独立运行）· `import_sqlite_to_mysql.py`（demo 数据入库 MySQL）。

## 开发

```bash
uv run pytest                     # 全量
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest tests/services/kb/  # 知识库
uv run pytest -k kb               # 所有知识库相关用例
```

代码结构：`trove/workflow/`（图 + 节点 + 意图路由 + 规则）、`trove/services/`（数据源 / 知识库 / SQL）、`trove/agent/`（会话编排）、`trove/llm/`（litellm 网关 / agent 循环 / 可观测）、`trove/storage/`（会话存储与检查点）、`trove/tracing/`（本地轨迹）、`trove/cli/`（REPL 与命令）。
