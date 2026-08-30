# Trove

**把数据库变成对话：自然语言提问，Trove 自动作答——而且越问越准。**

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2600%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()

---

Trove 是**自学习型对话数据代理**：自然语言提问，Trove 自动完成 schema 匹配、SQL 生成与自校验、执行与反思裁决，返回 Markdown 答案；答错时自动诊断根因、回滚并修正。

每次问答与修正都会沉淀为该数据源专属的知识库（注释、术语、参考 SQL、规则、经验教训），外加一套统一记忆子系统（情景记忆、自动偏好、自动晋升、用户画像），**越用越准**。

**语义优先（接入即建模、建模即保障）**：语义模型（`semantics.yml`）定义唯一可答边界——未覆盖的查询被拒绝并给出扩展模型的建议，绝不凭猜测作答；无语义模型的数据源会被整体拒绝并提示先 `/kb init`。

## 为什么选择 Trove

- **基于 LangGraph 构建**——`reflection` 工作流：意图路由、规划、agentic SQL 生成、确定性校验、执行与自校正闭环。内置三套工作流：`reflection`（默认）/ `fixed`（快速直通）/ `empty`（调试透传）。
- **Agentic SQL 生成**——ReAct 循环，模型持工具自校验（`validate_sql` / `probe_query` / `check_result` / `finish`）并自行判定结束；异常时优雅回退到经典「生成 → 校验重试」子图。
- **确定性安全护栏**——SQL 先过 AST 防火墙（只读语句白名单、DML 拦截、危险函数与元数据表阻断），再走规则链校验（形状 / 过滤 / 值域 / 排序），全程零 LLM。
- **自校正闭环**——失败时 LLM 判定根因，沿 `gen_sql → planner → schema_linking` 回滚阶梯重跑，带防环守护与 SQL 版本链（确定性回归反馈）。
- **会学习的知识库**——`/kb init` 起草 schema 注释与 `semantics.yml`（OSSIE 语义模型）、术语、模板；`/kb learn` 把确认后的问答沉淀为参考 SQL；修正闭环与评测失败自动蒸馏经验教训（Hint Bank）；检索以 schema linking 匹配为锚。
- **统一记忆子系统**——在知识库之外，跨会话记住每个用户：情景记忆（历史问题 + SQL + 结果裁决，注入生成上下文）、会话压缩时的自动用户偏好提取、观测回流（成功 → 待确认参考示例，修正/失败 → 待确认教训）、可选置信度自动晋升、schema 漂移检测、用户×数据源画像。自动内容一律先落 `pending`、经管理端确认后才进检索——绝不污染人工维护的知识库。
- **多候选共识**——更高温度生成备选 SQL，经 `select` 裁决投票，应对疑难问题。
- **人工确认（HITL）**——可选执行前确认（单任务批准/否决，批任务三选项）。
- **MCP 服务**——把 NL→SQL 暴露为工具（`ask_data` / `list_datasources` / `kb_status`）与资源（`trove://datasources`、`trove://<ds>/schema`、`trove://<ds>/semantics`），支持 stdio / SSE / streamable-http，可直接挂载到 Claude Code 等 MCP 客户端。
- **Web UI + REST API**——Vue 单页聊天界面（图表、分析过程、HITL 弹窗）+ `/v1` 纯 JSON API。
- **多数据源**——SQLite / PostgreSQL / MySQL / ClickHouse / DuckDB，按数据源隔离知识库，检索后端可选 `builtin` / `hybrid` / `rag`。
- **多模型 & 多语言**——litellm 网关（OpenAI / DeepSeek / Anthropic / 任意兼容 provider），交互语言统一 `zh` / `en`。
- **可观测**——每次运行产出本地 span 树轨迹（`/trace`，零外部依赖），也可输出完整 Langfuse trace（按节点、按调用拆分）。
- **默认高效**——确定性时间解析、复杂度分档 token 预算、确定性模板快径、精确结果缓存（重复问句 0 LLM 调用，命中跳过 HITL）。

## 快速开始

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖
uv sync

# 启动交互式 REPL（内置 BIRD 金融 demo 数据源）
uv run trove --datasource demo

# 一次性 CLI（JSON 输出，问题走 stdin）
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print
```

### REPL 命令

| 分组 | 命令 |
|---|---|
| 会话 | `/help` `/exit` `/clear` `/compact`（压缩历史）`/tasks`（别名 `/todo`） |
| 元数据 | `/tables` `/schemas` `/table_schema <表>` `/databases` `/kb …` `/trace` |
| 系统 | `/model [模型]` `/datasource [名]` `/init` `/facts`（用户记忆：list / add <文本> / del <id>） |

### CLI 参数

`--datasource/-d`（demo 或 `scheme://` URL）· `--config/-f` · `--model/-m` · `--print/-p`（JSON 输出）· `--workflow/-w`（reflection/fixed/empty）· `--version/-v`

## 界面

### Web UI（前后端分离）

后端 `trove serve` 是纯 JSON API（全部路径在 `/v1` 下，含 `/v1/chat` SSE 流式）；前端是独立构建的 SPA（Vue + Vite）。

```bash
# 后端（API，不含页面）
uv run trove serve --datasource demo

# 前端（本机开发：Vite dev server :5173，HMR + /v1 反代 → 127.0.0.1:8000）
cd frontend && npm run dev   # 打开 http://localhost:5173/

# 前端（生产构建产物 → frontend/dist/）
cd frontend && npm run build
```

特性：单页聊天（Vue 3 + Element Plus）、图表（折线/柱状/饼图，含主题化）、侧栏「分析过程」展示规划 / SQL / 校验链路、HITL 确认弹窗、服务端会话持久化、文件上传。

### MCP 服务

```bash
uv run trove mcp                                             # stdio（默认，本地挂载）
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>  # HTTP + bearer 鉴权
```

工具：`ask_data` · `list_datasources` · `kb_status`。资源（只读）：`trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`。

### Docker 部署

前端（nginx 托管 SPA + `/v1` 反代）与后端（纯 JSON API）为独立镜像，可独立构建、独立重启：

```bash
docker compose up --build        # 构建并启动（后端 :8000 仅供调试，前端 :8080）
docker compose build frontend    # 只重建前端镜像
docker compose restart backend   # 只重启后端
docker compose down
```

访问 `http://localhost:8080/`（默认登录：admin / `admin123`，仅本地演练；生产由 `TROVE_ADMIN_PASSWORD` 环境变量控制）。默认业务栈为 **PostgreSQL**（ParadeDB 镜像：pgvector + pg_bm25 共存），`db-init` 一次性服务灌入 BIRD 金融 demo 数据。真实对话需要 LLM 凭证：取消 `docker-compose.yml` 中 `~/.trove/conf` 只读挂载的注释，或在容器内提供 API key。

## 数据源

| 数据源 | 连接方式 | 安装 |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` / `sqlite://:memory:` | 内置 |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` / `duckdb://:memory:` | `uv sync --extra duckdb` |

数据源名 = 数据库名，每个库在 `.trove/kb/<数据库>/` 下独立演化知识库。驱动按需惰性加载。新增数据源：实现 `DatabaseAdapter` 六个抽象方法并在 `registry.py` 注册。

## 内部状态与存储

Trove 自身状态（会话、任务、用户事实、记忆情景/偏好、认证、设置、作业、血缘、检索日志、LangGraph 检查点）跑在统一 **StorageBackend** 抽象上（`trove/storage/backends/`）：

- **生产 = PostgreSQL**：设置 `TROVE_STORAGE_URL=postgresql://…`（或直接用 compose 的 Postgres 栈）。所有内部 store + LangGraph checkpointer（`AsyncPostgresSaver`）落在同一 PG 实例。
- **测试 / 本地 = 内存 SQLite 兜底**：未设 `TROVE_STORAGE_URL` 时，同一份 store 代码跑在内存 `SqliteBackend` 上——零网络、零 key，保持测试自包含。
- 后端自动翻译 SQLite 方言（`?`→`%s`、`lastrowid`→`RETURNING id`、`AUTOINCREMENT`→`IDENTITY`），store 只需一套可移植代码。
- **不在该抽象上**：KB 镜像（`kb.sqlite`、FTS5）留在 SQLite 兜底（PG 生产走 `pg_hybrid` 检索后端），业务数据源适配器仍是用户自己的数据库。

真实 Postgres 覆盖为 env-gated 集成测试（`-m integration`、`PG_TEST_URL`）——见 `tests/storage/test_pg_storage.py` 与 `.github/workflows/backend.yml`。

## 配置

配置优先级（模型选择）：CLI `--model` > `conf/agent.yml` / `~/.trove/conf/agent.yml`。

```yaml
# conf/agent.yml 示例
agent:
  target: deepseek/deepseek-reasoner      # litellm 模型字符串（推理模型亦可）
  model_fast: deepseek/deepseek-chat      # 快速档模型：simple/standard 复杂度查询
  node_models:                            # 每节点模型覆盖（优先于复杂度分档）
    planner: deepseek/deepseek-chat
    reflect: deepseek/deepseek-reasoner
  language: zh                            # 交互语言 zh / en（默认 zh）
  semantic_first: true                    # 语义优先：语义模型是唯一可答边界
  hitl: false                             # 执行前人工确认
  insights: true                          # 执行后 LLM 生成洞察
  conclusion: true                        # 一句话结论摘要，置于回答开头
  result_cache: true                      # 精确结果缓存：重复问句 → 0 LLM 调用
  fast_path: true                         # 确定性模板快径
  reflect_skip: standard                  # validate 规则全过后跳过 LLM 裁决
  memory:                                 # 统一记忆子系统（见「记忆」章节）
    enabled: true
    episodes: true                        # 跨会话情景记忆
    auto_examples: true                   # 成功 → 待确认参考示例
    auto_preferences: true                # 压缩时抽取用户口径/偏好
    promotion: false                      # 可选：按置信度自动确认教训
    promotion_threshold: 0.8
    profile_boost: false                  # 可选：注入用户×数据源失败画像
    schema_drift_check: true
    # retention_days: {episodes: 180, preferences: 90, facts: 180, retrieval_log: 90}
  providers:
    - name: openai                        # 非官方端点（兼容 OpenAI API）
      litellm_params:
        api_key: ${OPENAI_API_KEY}
        api_base: https://your-endpoint/v1
```

API key：写入项目根 `.env`（启动自动加载，已 gitignore），或直接 export 环境变量（如 `DEEPSEEK_API_KEY`）。`providers[].litellm_params` 会按模型前缀透传给 litellm。

**可观测性（Langfuse）**：开启 `agent.observability.tracing.enabled: true`，并在 `.env` 提供 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`。每次问答是一棵完整 trace 树（每节点一个 span、每次 LLM 调用一条 generation，含推理过程），按 `session_id`、`node`、`question` 分组。本地 `/trace` 视图始终可用，不依赖外部服务。

## 安全（只读执行）

Trove 在应用层做了纵深防御——AST 防火墙（`trove/services/sql/guard.py`）：只读语句白名单、data-modifying CTE 拦截、危险函数与元数据表拒绝、可选表名 allowlist。**应用层不是安全边界**，请务必在数据库侧使用**专用只读角色**：

```sql
-- PostgreSQL（PG14+，覆盖未来新建对象）
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL（按库授权，固定来源 IP）
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

要点：

- 隐藏敏感列用**列级 grant 或视图**，在授权层做，别依赖应用层掩码。
- 连接侧开超时：`statement_timeout` / `lock_timeout`（PG）、`MAX_EXECUTION_TIME`（MySQL 会话变量）。
- 行数上限在数据库侧用 `LIMIT` / `LEAST()` 强制；应用层限制可被 SQL 绕过。
- 可选 **EXPLAIN 行数估算守卫**（`explain_row_guard: true`）：执行前 `EXPLAIN` 估算最重算子行数，超限打回加 LIMIT/收窄；方言不可解析时 fail-open。
- 多租户优先每租户独立库 + 独立只读角色；共享库时用 RLS 或程序化 CTE 预过滤。
- 所有只读执行工具（`probe_query` / `check_result` / `explain_plan` / `search_values`）统一先过 AST 防火墙，每次调用与结果落审计日志（`sql_audit`）。

## 知识库（如何学习）

> 平台化部署（`serve`）下初始化走管理端 `kb/init`；以下为 REPL 本地流程。

1. `/kb init`——LLM 起草表/列注释（大 schema 分块起草后合并），可选 `--docs <dir>` 导入官方列描述，术语与参考模板由确定性规则生成。
2. 编辑 `.trove/kb/<数据源名>/` 下的 YAML，补充口径、术语与 term→SQL 映射。
3. `/kb reload`——立即生效。
4. 正常提问；满意的问答用 `/kb learn` → 审阅草稿 → `/kb learn --yes` 沉淀为参考 SQL。
5. `/kb list`——查看各数据源知识条目数。

**Hint Bank（经验库）**：修正闭环成功后自动沉淀待确认教训；`/kb lessons` 查看、`/kb lessons --yes` 确认入库。批量制造：`scripts/distill_lessons.py` 从 eval 失败记录逐条提炼经验。

知识库按数据源隔离；YAML 是唯一事实源（可 git 管理），SQLite 镜像仅供运行时检索。

## 记忆（跨会话如何记住）

在数据源级知识库之外，Trove 提供统一记忆门面（`trove/services/memory/`），把「提问 → 修正 → 未来回答」串成闭环。所有自动内容一律先落 `pending`、经管理端确认后才进检索——自动记忆绝不绕过人工把关。

| 层 | 存什么 | 存哪 | 怎么用 |
|---|---|---|---|
| **情景记忆** | 每用户×数据源的 `问题 → SQL → 结果裁决 → 修正要点 → 命中表` | `~/.trove/memory/episodes.sqlite` | 确定性相关度门（≥0.5）+ 最近度排序，注入 gen_sql 上下文块——成功 SQL = few-shot 锚，失败+修正 = 反例 |
| **自动偏好** | 会话压缩时抽取的持久口径/偏好（「营收 = 净收入」「用 30 日均值」） | 高置信 → `user_facts.db`；低置信 → 待确认草稿（`preferences.sqlite`） | 注入个性化上下文；草稿在管理端确认 |
| **观测回流** | 成功 → 待确认参考示例（`tags: [auto]`）；修正 → 待确认 Hint Bank 教训；失败 → LLM 蒸馏待确认教训 | `examples.yml` / `lessons.yml`（pending） | 确认后才进检索（`/kb examples/lessons --yes` 或管理端） |
| **自动晋升**（可选） | 置信度累加器，跨过 `promotion_threshold` 自动确认教训 | `lessons.yml` 的 `confidence` 字段 | 仍可审计/回退 |
| **Schema 漂移** | live schema 与 KB `schema_notes.yml` 的表/列差异（零 LLM） | 生命周期扫描报告 | 提醒重跑 `/kb init` |
| **画像** | 每用户×数据源的正确率/失败模式/已确认偏好 | 由 episodes + facts 聚合 | 管理端 `GET /admin/memory/profile` |

在 `conf/agent.yml` 的 `agent.memory` 下开关与调参（`episodes`、`auto_examples`、`auto_preferences`、`promotion` 默认关、`profile_boost` 默认关、`retention_days`）。生命周期清理由周期 maintenance sweep 自动执行。

## 评测

BIRD 开发集执行准确率（EX），跑真实数据源 + 完整 reflection 管线：

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

答错题写入 `.trove/eval/failures.jsonl` 供蒸馏经验。辅助脚本：`import_golden_examples.py` · `import_bird_descriptions.py` · `probe_enums.py` · `import_sqlite_to_mysql.py` · `eval_hybrid_retrieval.py` · `tune_rrf.py` · `offline_eval.py`。

## 开发

```bash
uv run pytest                     # 全量（~2660 测试，mock LLM，零网络零 key）
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest tests/services/kb/  # 知识库
uv run pytest tests/services/memory/  # 统一记忆子系统
uv run pytest -k kb               # 所有知识库相关用例
```

代码结构：`trove/workflow/`（图 + 节点 + 意图路由 + 规则）· `trove/services/`（数据源 / 知识库 / SQL / **记忆**）· `trove/agent/`（会话编排）· `trove/llm/`（litellm 网关 / agent 循环 / 可观测）· `trove/storage/`（会话存储与检查点）· `trove/tracing/`（本地轨迹）· `trove/cli/`（REPL 与命令）。

## 文档

- `CLAUDE.md`——架构总览、工作流分层与硬性项目约束
- `conf/agent.yml`——带注释的参考配置
- `/v1/docs`——REST API 文档（`serve` 运行时可用）

## 贡献

欢迎提交 bug 报告、功能建议、文档与 Pull Request。请遵循仓库约定、保持改动聚焦，并在提交前确保测试通过。

## License

Trove 以 [Apache License 2.0](LICENSE) 开源。
