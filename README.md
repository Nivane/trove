# Trove

Trove — Ask your data, answered by a knowledge base that grows.

基于 LangGraph 的数据问答 agent：自然语言 → schema 匹配 → SQL 生成（自校验重试）→ 执行 → 反思（自校正）→ Markdown 答案。每个数据源拥有自己的知识库（表/列注释、业务术语、参考 SQL、模板），随使用不断演化。

## 功能特性

- **LangGraph 流水线**：`schema_linking → gen_sql（校验重试子图）→ execute_sql → reflect（≤2 次回跳）→ output`
  - 三个工作流：`reflection`（默认，带自校正）/ `fixed`（快速直通）/ `empty`（调试透传）
- **优雅降级**：SQL 生成/执行失败时输出可读的错误说明，不中断会话
- **流式输出**：REPL 实时显示 thought / SQL / 结果 / 答案；Ctrl+C 可取消运行中的查询
- **双轨持久化**：会话消息（`~/.trove/sessions/`）+ 图状态检查点（`~/.trove/checkpoints.db`，支持时间旅行）
- **可演化知识库**（按数据源隔离，`.trove/kb/<datasource>/`）：
  - `schema_notes.yml` — 表/列注释、指标口径（`/kb init` 生成骨架）
  - `semantics.yml` — 业务术语 → 物理映射（中文问题匹配、口径统一）
  - `examples.yml` — 参考 SQL + 模板（few-shot 注入 gen_sql）
  - `/kb learn` 半自动演化：LLM 起草 → 人工确认 → 入库
- **多模型**：litellm 网关，支持任意兼容 provider（OpenAI / DeepSeek / Anthropic / …），可配 `api_base` 接非官方端点

## 快速开始

```bash
uv sync          # 安装依赖（含 dev 测试工具）
uv run pytest    # 全量测试（mock LLM，零网络零 key）

# 交互式 REPL（内置 BIRD 金融 demo 数据源）
uv run trove --datasource demo

# 一次性 JSON 输出（问题走 stdin）
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print
```

REPL 命令：`/help` `/tables` `/table_schema <表>` `/databases` `/datasource <名>` `/kb init|list|reload|learn` …

## 配置

配置优先级（模型选择）：CLI `--model` > `conf/agent.yml` / `~/.trove/conf/agent.yml`。

```yaml
# conf/agent.yml 示例
agent:
  target: deepseek/deepseek-chat    # litellm 模型字符串
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

启用后每个 LLM 调用（planner 计划 / gen_sql 生成与修正 / reflect 裁决）都会进入 Langfuse，prompt 与输出全程可见，并按 `session_id`、`node`、`question` 元数据分组——CoT 每一步可回溯。

## 知识库使用

1. 启动 REPL 后 `/kb init` —— 从当前数据源 schema 生成注释骨架到 `.trove/kb/<数据源名>/schema_notes.yml`
2. 编辑 YAML 补充表/列描述、业务术语（term → SQL 表达式映射）
3. `/kb reload` 立即生效
4. 正常提问；满意的问答用 `/kb learn` → 审阅草稿 → `/kb learn --yes` 沉淀为参考 SQL
5. `/kb list` 查看各数据源知识条目数

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

## 开发

```bash
uv run pytest                     # 全量
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest tests/services/kb/  # 知识库
uv run pytest -k kb               # 所有知识库相关用例
```

代码结构：`trove/workflow/`（图 + 节点）、`trove/services/`（数据源 / 知识库 / SQL）、`trove/agent/`（会话编排）、`trove/storage/`（会话存储与检查点）、`trove/cli/`（REPL 与命令）。
