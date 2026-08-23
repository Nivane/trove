# Trove

Trove — Talk to your data.
It answers, and learns with every question.

把数据库变成对话：自然语言提问，Trove 自动完成 schema 匹配、SQL 生成与自校验、执行与反思裁决，输出 Markdown 答案，答错自动诊断根因并回滚修正。

它边用边学——每次问答与修正沉淀为该数据源的专属知识（注释、术语、参考 SQL、规则、经验），冷启动靠规则兜底，越用越准。

## 功能特性

- **LangGraph 流水线**（`reflection` 工作流，全链路）：
  `route_intent → parse_date → schema_linking → planner → gen_sql → execute_sql → select → validate → reflect → (analyze_error 回滚) → output`
  - **意图路由**：LLM 判定 + 规则校验，五意图分流（优先级 write > metadata > chitchat > correction）——`write`（写操作请求直接拒绝兜底）/ `chitchat`（问候闲聊短路，不烧 LLM）/ `correction`（纠错·追问重写后再次路由）/ `query`（数据查询）/ `metadata`（元数据问答：`answer_metadata → metadata_check` 自校验循环）
  - **gen_sql（agentic 默认）**：ReAct 循环，模型持工具自校验、自行判定结束（≤6 轮）；异常或空产出自动回退经典「生成 → 校验重试」子图；KB 精确命中（词重叠 ≥0.95）时直接采用标准 SQL，跳过生成。工具面：`validate_sql`（语法校验 + 静态语义启发式警告）、`probe_query`（只读执行观测，10 行/5s）、`check_result`（确定性规则链校验——F1 形状 / F2 过滤 / F3 值域 / F4 排序，首败即止；**通过即 harness 自动定稿**，无需显式 finish）、`search_values`（值查找）、`explain_plan`（EXPLAIN 执行计划，只读毫秒级，定稿前发现慢查询写法）、`lookup_schema`（预算裁剪漏掉的表按需取结构）、`finish(answer)`（显式定稿协议）
  - **多候选共识**：备选候选以更高温度生成 → `select` 裁决（KB 命中时跳过）
  - **analyze_error 根因诊断**：LLM 判定失败根因，沿 `gen_sql → planner → schema_linking` 回滚阶梯重跑，防环守护；reflect 裁决与执行错误共享 ≤10 轮修正上限；SQL 版本链记录每轮失败（SQL + 结果签名），与上一版对比产生确定性回归反馈（无效修复 / 无进展 / 问题转移）
  - **会话内任务层（跨轮）**：规则门控的 LLM 拆解（命中「依次 / 分别 / 还要 / 编号列表」等提示词才花一次拆解调用，单问题零额外 token）→ 逐条顺序执行（单条失败不中断批次）→ 跨轮推进：回复「继续 / 重做 / 跳过 / 追加」被解释为任务操作；批处理 HITL 给三选项（仅当前 / 确认全部 / 不继续）；REPL `/tasks`（别名 `/todo`）与 Web UI 任务面板查看进度
  - **精确结果缓存**：同会话内归一化后完全相同的问句直接返回上次已验证的 SQL + 结果（0 LLM 调用），TTL 300s、按数据源隔离；命中跳过 HITL 确认（该 SQL 首轮已人工确认过）
  - **复杂度分档与确定性快径**：`grade_complexity` 分 simple / standard / complex 三档——档位化 token 预算 + 分档选模（simple/standard → `model_fast`，complex → `target`）+ 确定性模板快径（单表/单聚合模板命中即直接产出 SQL，跳过 planner/生成/裁决）+ `reflect_skip`（validate 确定性规则全过即跳过 reflect 的 LLM 裁决）
  - 三个工作流：`reflection`（默认，带自校正）/ `fixed`（快速直通）/ `empty`（调试透传）
- **确定性时间解析**（parse_date）：相对时间表达（"最近7天" / "last week"）解析为绝对范围，未命中静默透传
- **上下文预算**：gen prompt 的可选块（示例/规则/术语/经验/计划/历史）按优先级装入 token 预算（simple 瘦身 / complex 放开，默认 2500），实际装载量进入可观测
- **优雅降级**：SQL 生成/执行失败时输出可读的错误说明，不中断会话
- **流式输出**：REPL 实时显示 thought / SQL / 结果 / 答案；Ctrl+C 可取消运行中的查询
- **双轨持久化**：会话消息（`~/.trove/sessions/`）+ 图状态检查点（`~/.trove/checkpoints.db`，支持时间旅行）
- **本地轨迹**：每次运行记录 span 树轨迹（节点耗时、每次 LLM 调用输入输出、工具调用）到 `~/.trove/traces.jsonl`，`/trace` 回放完整推理链路，零外部依赖；每次问答附 token 用量与耗时统计
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
| 会话 | `/help` `/exit` `/clear` `/compact`（压缩历史）`/tasks`（任务清单，别名 `/todo`） |
| 元数据 | `/tables` `/schemas` `/table_schema <表>` `/databases` `/kb …` `/trace` |
| 系统 | `/model [模型]` `/datasource [名]` `/init` |

CLI 参数：`--datasource/-d`（demo 或 `scheme://` URL）· `--config/-f` · `--model/-m` · `--print/-p`（JSON 输出）· `--workflow/-w`（reflection/fixed/empty）· `--version/-v`

## Web UI（前后端分离）

后端 `trove serve` 是纯 JSON API（全部路径在 `/v1` 下，含 `/v1/chat` SSE 流式），**不托管前端页面**。前端是独立构建的 SPA（Vue + Vite，`frontend/`），发布只依赖 CDN/nginx：

```bash
# 后端（API，不含页面）
uv run trove serve --datasource demo

# 前端（本机开发：Vite dev server，:5173，HMR + 反代 /v1 → 127.0.0.1:8000）
cd frontend && npm run dev
# 打开浏览器 → http://localhost:5173/

# 前端（生产构建产物）
cd frontend && npm run build   # 产物在 frontend/dist/
```

- 单页聊天界面（Vue 3 + Element Plus，零后端页面依赖）
- **HITL 确认框**：执行前展示 SQL + 语义说明，单任务提供 批准/否决，批任务提供三选项（仅当前 / 确认全部 / 不继续）
- **图表与分析过程**：结果可渲染折线/柱状/饼图（含主题化），侧栏「分析过程」展示规划、SQL 与校验链路；输入框加号菜单可切换图表类型 / 上传文件
- 接口：`POST /v1/chat`（SSE 流式）、`GET/POST/DELETE /v1/sessions[/{id}]`（列表分页 `limit/offset`）、`POST /v1/sessions/{id}/title`（重命名）、`GET /v1/sessions/{id}/tasks`、`POST /v1/sessions/{id}/resume`（HITL 继续）、`POST /v1/sessions/{id}/compact|clear`、`GET /v1/catalog/*`（含 `POST /v1/catalog/upload` 数据上传）、`GET/POST /v1/kb/*`、`/v1/admin/*`（用户 / 数据源 / KB / 审计管理）；API 文档见 `/v1/docs`
- 会话 ID 与界面语言（zh/en）保存在浏览器 localStorage；对话历史由服务端按 session 持久化
- 停止按钮只中断客户端读取——服务端会跑完本次查询并持久化，刷新页面即可看到结果

## Docker 部署（前后端独立容器）

前端容器（nginx 托管 SPA + 反代 `/v1` → 后端）与后端容器（`trove serve`，纯 JSON API）各自独立镜像、独立重建，互不阻塞：

```bash
docker compose up --build        # 构建并启动（后端 :8000 仅供调试，前端 :8080）
docker compose build frontend    # 只重建前端镜像（前端改动不动后端）
docker compose restart backend   # 只重启后端，前端容器不受影响
docker compose down              # 停止并移除容器
```

- 访问 `http://localhost:8080/`；登录：admin / `admin123`（compose 里 `TROVE_ADMIN_PASSWORD` 仅本地演练；生产由环境变量控制）
- compose 已显式 `--datasource demo`（内置 BIRD 金融 SQLite，演练入口）；生产数据源改由管理端注册（见下节）
- 真实对话需要 LLM 凭证：取消 compose 中 `~/.trove/conf` 只读挂载的注释，或在容器内提供 API key（无凭证时 `kb/init` 会报凭证错误）

## 平台化数据源与知识库管理

`serve` **默认零数据源启动**（不注册任何源），数据源与 KB 由管理端全生命周期管理，持久化到 `.trove/datasources.yml` + `.trove/kb/`（重启自动恢复）：

1. admin 登录 → 管理端「数据源」注册（内置 demo 或 `scheme://` URL，注册即连接探测，失败 400 报原因）
2. 注册后 `kb/init`（LLM 起草 schema 注释 + 确定性 terms/templates；无 LLM 凭证时按配置走纯骨架或报凭证错误）
3. 用户端下拉/列表仅显示「已连接且 KB 已初始化」的数据源；非 admin 用户还需管理端 grants 授权
4. `/kb learn` 半自动演化与 REPL 相同；`kb/reload` 使编辑立即生效

管理端点：`GET/POST/DELETE /v1/admin/datasources[/{name}]`、`POST /v1/admin/datasources/{name}/reconnect|kb/init|kb/reload`、`GET /v1/admin/users/{user_id}/datasources`（grants）。

## 配置

配置优先级（模型选择）：CLI `--model` > `conf/agent.yml` / `~/.trove/conf/agent.yml`。

```yaml
# conf/agent.yml 示例
agent:
  target: deepseek/deepseek-v4-flash    # litellm 模型字符串（推理模型如 deepseek-reasoner 亦可）
  model_fast: deepseek/deepseek-chat    # 快速档模型：simple/standard 复杂度查询的生成/裁决/语义/洞察走此模型（留空 = 不分档）
  language: en                          # 交互语言 en / zh：提示词、答案、轨迹统一使用
  date_parser: true                     # 确定性相对时间解析（未命中静默透传）
  explain_semantics: false              # 生成 SQL 后 LLM 说明语义（输出与 HITL 确认时展示）
  hitl: false                           # 执行前人工确认（LangGraph interrupt，需 persistence/checkpointer；批任务给三选项）
  insights: false                       # 执行后 LLM 基于结果生成洞察
  conclusion: false                     # 执行后 LLM 用一句话生成结论摘要，置于回答开头（结论前置）
  result_cache: false                   # 精确结果缓存（进程内存）：同问句直接返回上次已验证结果，0 LLM，命中跳过 HITL
  fast_path: true                       # 确定性模板快径：单表/单聚合模板命中即出 SQL，跳过 planner/生成/裁决
  reflect_skip: simple                  # validate 规则全过后跳过 LLM 裁决：simple / standard / all / off
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

启用后每次问答是一棵完整的 trace 树：每个节点（意图 / 计划 / gen_sql / 裁决 / 错误诊断…）一个 span，节点内每次 LLM 调用（含推理模型的 reasoning 过程）记 generation，**失败 generation 记 ERROR 级别**；非 LLM 步骤同样插桩——SQL 执行、每轮工具调用（probe / check / explain / search_values 的入参与观测，工具出错记 ERROR）、KB 检索、规则校验、结果缓存命中、终态 summary 均有独立 span。全部按 `session_id`、`node`、`question` 元数据分组，CoT 每一步可回溯。本地轨迹（`/trace`）始终可用，不依赖外部服务。

## 知识库使用

> 平台化部署（`serve`）下初始化走管理端 `kb/init`（见「平台化数据源与知识库管理」）；以下为 REPL 本地流程。

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
- `serve` 零默认启动：未指定 `--datasource` 时从 `.trove/datasources.yml` 恢复已注册源（失败跳过并在管理端显示断开态）；REPL/CLI 用 `--datasource demo` 或 `scheme://` URL 直接指定
- 新增适配器：实现 `DatabaseAdapter` 五个抽象方法（`trove/services/datasource/adapters/base.py`），在 `registry.py` 的 `_ADAPTER_REGISTRY` 注册

**真实服务集成测试**（未设置环境变量时自动跳过，CI 零网络约束不变）：

```bash
MYSQL_TEST_URL="mysql://user:pass@host:3306/anydb" \
  uv run pytest tests/services/test_mysql_adapter.py -m integration

CLICKHOUSE_TEST_URL="clickhouse://default:pass@host:8123/default" \
  uv run pytest tests/services/test_clickhouse_adapter.py -m integration
```

### 只读执行安全（多租户 SaaS / 共享库部署必读）

Trove 对自动生成的 SQL 做了应用层纵深防御（`trove/services/sql/guard.py` 的 AST
防火墙：只读语句白名单、data-modifying CTE 拦截、危险函数、元数据表拒绝、可选
表名 allowlist），但**应用层可被绕过，不是安全边界**。真正的边界在数据库侧：
给 Trove 用的连接必须指向一个**专用只读角色**，授权粒度比应用层更细。

PostgreSQL（PG14+，覆盖未来新建对象）：

```sql
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;   -- 或按 schema 显式授权:
-- GRANT CONNECT ON DATABASE app TO trove_ro;
-- GRANT USAGE ON SCHEMA public TO trove_ro;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO trove_ro;
```

MySQL（按库授权，禁止 `*.*`，固定来源 IP）：

```sql
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

要点：

- 隐藏敏感列用**列级 grant 或视图**，在授权层做，别依赖应用层掩码
- 连接侧开启超时：`statement_timeout`/`lock_timeout`（PG）、`MAX_EXECUTION_TIME`
  （MySQL 会话变量）
- 行数上限在数据库侧用 `LIMIT`/`LEAST()` 强制，应用层限制可被 SQL 绕过
- 多租户优先每租户独立库 + 独立只读角色；必须共享库时用 RLS 或程序化 CTE 预过滤
- 只读角色的 DSN 密钥妥善保管；Trove 报错路径已统一脱敏（`sanitize_error_text`），
  但错误日志仍可能泄露连接信息——日志系统同样需要访问控制
- 所有只读执行工具（`probe_query` / `check_result` / `explain_plan` / `search_values`）
  统一先过 AST 防火墙（含表名 allowlist），每次调用与结果落审计日志（`sql_audit`）

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
