# Trove

**把数据库变成对话：自然语言提问，Trove 自动作答——而且越问越准。**

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2600%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()

---

Trove 是**自学习型对话数据代理**：自然语言提问，返回有真实 SQL 支撑的 Markdown 答案。它的承诺不是「永远答对」，而是「**错了绝不轻易放过**」——会校验、会拒绝、会学习。

**语义层划定边界。** 语义模型（`semantics.yml`）是唯一可答范围；未覆盖的查询会被拒绝并建议扩展模型，而不是凭猜测作答。**RAG 提供弹药。** 每次生成都以专属知识库为支撑——schema 注释、术语、参考 SQL、规则、经验、跨会话记忆，围绕问题确定性检索。

**自校验闭环保证正确。** 生成的 SQL 经过零 LLM 规则链（形状 / 过滤 / 值域 / 排序）与反思裁决；答错时诊断根因、回滚并修正，跨版本回归硬校验。简单查询走确定性快径；复杂查询升级为 ReAct 智能体循环，先探测、自证后作答。

**学习沉淀为资产。** 每次修正蒸馏为 Hint Bank 经验，每个确认的问答成为参考 SQL，跨会话记忆回溯情景与偏好——越用越准，且知识归你所有。**治理让它可落地。** 自动学习内容一律先落 `pending`、管理端确认后才生效，支持人工审批（HITL），每次工具执行都留有审计记录。

## 亮点

- **LangGraph reflection 工作流** —— 意图路由 → 规划 → agentic SQL 生成（ReAct 循环 + 工具自校验）→ 执行 → 反思裁决 → 自校正闭环
- **确定性安全护栏** —— AST 防火墙（只读语句白名单、DML 拦截、危险函数阻断）+ 零 LLM 规则链（形状 / 过滤 / 值域 / 排序）
- **会学习的知识库** —— `/kb init` 起草 schema 注释与语义模型；`/kb learn` 把确认后的问答沉淀为参考 SQL；修正闭环自动蒸馏 Hint Bank 经验
- **统一记忆** —— 跨会话情景记忆、自动用户偏好提取、用户×数据源画像；自动内容一律先落 `pending`、管理端确认后才生效
- **MCP 服务** —— 把 NL→SQL 暴露为工具与资源，stdio / SSE / streamable-http，可直接挂载到 Claude Code 等 MCP 客户端
- **Web UI + REST API** —— Vue 单页聊天（图表、分析过程、HITL 弹窗）+ `/v1` JSON API
- **多数据源** —— SQLite / PostgreSQL / MySQL / ClickHouse / DuckDB，各自独立知识库
- **多模型 & 多语言** —— litellm 网关（OpenAI / DeepSeek / Anthropic / 任意兼容 provider），交互语言统一 `zh` / `en`

## 快速开始

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖
uv sync

# 交互式 REPL（内置 BIRD 金融 demo 数据源）
uv run trove --datasource demo

# 一次性 CLI（JSON 输出，问题走 stdin）
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print
```

### REPL 命令

| 分组 | 命令 |
|---|---|
| 会话 | `/help` `/exit` `/clear` `/compact` `/tasks` |
| 元数据 | `/tables` `/schemas` `/table_schema <表>` `/databases` `/kb …` `/trace` |
| 系统 | `/model [模型]` `/datasource [名]` `/init` `/facts` |

### CLI 参数

`--datasource/-d` · `--config/-f` · `--model/-m` · `--print/-p` · `--workflow/-w` · `--version/-v`

## 界面

### Web UI（前后端分离）

后端是纯 JSON API（全部路径在 `/v1` 下，含 SSE 流式对话）；前端是独立构建的 SPA（Vue + Vite）。

```bash
uv run trove serve --datasource demo      # 后端（API，不含页面）
cd frontend && npm run dev                # 本机开发 → http://localhost:5173/
cd frontend && npm run build              # 生产构建产物 → frontend/dist/
```

### MCP 服务

```bash
uv run trove mcp                                                            # stdio（默认，本地挂载）
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

工具：`ask_data` · `list_datasources` · `kb_status`。资源（只读）：`trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`。

### Docker 部署

前端（nginx 托管 SPA + `/v1` 反代）与后端（纯 JSON API）为独立镜像，可独立构建、独立重启：

```bash
docker compose up --build        # 构建并启动（前端 :8080，后端 :8000）
docker compose build frontend    # 只重建前端镜像
docker compose restart backend   # 只重启后端
docker compose down
```

访问 `http://localhost:8080/`（默认登录：admin / `admin123`，仅本地演练；生产由 `TROVE_ADMIN_PASSWORD` 环境变量控制）。compose 默认业务栈为 PostgreSQL（ParadeDB 镜像），`db-init` 服务预灌 BIRD 金融 demo 数据。真实对话需要 LLM 凭证：取消 `docker-compose.yml` 中 `~/.trove/conf` 只读挂载的注释，或在容器内提供 API key。

## 数据源

| 数据源 | 连接方式 | 安装 |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` / `sqlite://:memory:` | 内置 |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| Doris | `doris://user:pass@host:9030/database` | `uv sync --extra doris` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` / `duckdb://:memory:` | `uv sync --extra duckdb` |

数据源名 = 数据库名，每个库在 `.trove/kb/<数据库>/` 下独立演化知识库。新增数据源：实现 `DatabaseAdapter` 抽象方法并在 `registry.py` 注册。

## 配置

优先级：CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`。完整选项见带注释的参考配置 `conf/agent.yml`：

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm 模型字符串
  language: zh                         # 交互语言 zh / en
  semantic_first: true                 # 语义模型是唯一可答边界
  memory:
    enabled: true                      # 统一记忆子系统
```

API key：写入项目根 `.env`（启动自动加载，已 gitignore），或直接 export 环境变量（如 `DEEPSEEK_API_KEY`）。自定义 OpenAI 兼容端点配置在 `agent.providers`；可选 Langfuse 追踪：`agent.observability.tracing.enabled: true` + `LANGFUSE_*` 环境变量。

## 安全（只读执行）

Trove 内置 AST 防火墙（只读语句白名单、DML 拦截、危险函数与元数据表阻断）与可选 EXPLAIN 行数估算守卫。**应用层不是安全边界**，请务必在数据库侧使用专用只读角色：

```sql
-- PostgreSQL（覆盖未来新建对象）
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL（按库授权，固定来源 IP）
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

另建议：敏感列用列级 grant 或视图隐藏，连接侧设置 `statement_timeout` / `lock_timeout`（PG）或 `MAX_EXECUTION_TIME`（MySQL），行数上限在数据库侧用 `LIMIT` / `LEAST()` 强制。所有执行工具调用统一落审计日志（`sql_audit`）。

## 如何学习

> 平台化部署（`serve`）下初始化走管理端 API；以下为 REPL 本地流程。

1. `/kb init` —— LLM 起草表/列注释与语义模型（可选 `--docs <dir>` 导入官方列描述），术语与模板由确定性规则生成。
2. 编辑 `.trove/kb/<数据源名>/` 下的 YAML，补充口径、术语与 term→SQL 映射。
3. `/kb reload` —— 立即生效。
4. 正常提问；满意的问答用 `/kb learn` → 审阅草稿 → `/kb learn --yes` 沉淀为参考 SQL。
5. `/kb list` —— 查看各数据源知识条目数。

修正闭环自动沉淀待确认 Hint Bank 教训（`/kb lessons` 查看、`/kb lessons --yes` 确认入库）。在数据源知识库之外，统一记忆子系统跨会话记住每个用户（情景记忆、自动偏好、画像）——自动内容一律先落 `pending`、管理端确认后才生效。YAML 是唯一事实源（可 git 管理），SQLite 镜像仅供运行时检索。

## 开发

```bash
uv run pytest                     # 全量（~2660 测试，mock LLM，零网络零 key）
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest -k kb               # 所有知识库相关用例
```

代码结构：`trove/workflow/`（图、节点、规则）· `trove/services/`（数据源 / 知识库 / SQL / 记忆）· `trove/agent/`（会话编排）· `trove/llm/`（litellm 网关 / agent 循环）· `trove/storage/`（会话与检查点）· `trove/cli/`（REPL 与命令）。更深入的架构说明见 `CLAUDE.md`；REST API 文档在 `serve` 运行时的 `/v1/docs`。

## 评测

BIRD 开发集执行准确率（EX），跑真实数据源 + 完整 reflection 管线：

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

答错题写入 `.trove/eval/failures.jsonl`，可用 `scripts/distill_lessons.py` 批量蒸馏为经验。

## 贡献

欢迎提交 bug 报告、功能建议、文档与 Pull Request。请保持改动聚焦，并在提交前确保测试通过。

## License

Trove 以 [Apache License 2.0](LICENSE) 开源。
