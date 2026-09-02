# Quick Start: 接入 MySQL / Apache Doris 数据源

本指南覆盖两种接入方式：

- **平台方式**（推荐给团队）：`docker compose` 起前后端，admin 在 Web 管理台注册数据源 → `kb/init` → 用户端可见可问。
- **本地方式**（推荐给开发/联调）：`uv run trove` REPL 直接挂数据源，`/kb init` 后立即提问。

MySQL 与 Doris 走同一 MySQL 线协议驱动（`aiomysql`），接入流程完全一致，仅 URL scheme / 端口 / 方言不同。

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| Python | >= 3.12（项目用 `uv` 管理） |
| Node | >= 22.22（仅平台方式需要前端） |
| LLM 凭证 | `DEEPSEEK_API_KEY`（或其它 provider，见 `conf/agent.yml`）—— `/kb init` 起草注释、`/kb learn`、SQL 生成都需要；没有凭证也能跑（纯骨架 + 确定性 terms/templates，但准确性有限） |
| 数据库 | 一个可连的 MySQL（默认 3306）/ Doris（默认 9030）实例，**建议只读账号** |

## 1. 安装依赖

```bash
uv sync --extra mysql --extra doris     # 业务驱动（aiomysql）
# 平台方式前端另行构建：
# cd frontend && npm ci
```

> Doris 复用 MySQL 协议驱动，`--extra doris` 即 `aiomysql`。装一次两库都能连。

## 2. 接入 MySQL

**平台方式**（管理台）：

```bash
docker compose up --build          # 前端 :8080，后端 :8000（调试）
# 浏览器 http://localhost:8080，登录 admin / admin123
```

管理台流程：`数据源` → `注册` → 填连接串 → 该源 `kb/init`（异步，LLM 起草 schema 注释 + 确定性 terms/templates）→ 给普通用户 `授权`（admin → 数据源授权）。用户端下拉即见。

**本地方式**（REPL）：

```bash
uv run trove --datasource mysql://trove_ro:pass@127.0.0.1:3306/financial
```

REPL 里执行：

```
/kb init          # 起草 schema 注释 + 语义模型 + terms/templates
/kb reload        # 应用 YAML
/kb list          # 确认该源 KB 已建
```

然后直接提问。REPL 帮助 `/help` 查看命令。

## 3. 接入 Apache Doris

Doris 的接入与 MySQL **完全同流程**，差异点只有三个：

| 项 | MySQL | Doris |
|---|---|---|
| URL scheme | `mysql://` | `doris://` |
| 默认端口 | 3306 | 9030（FE 查询口） |
| 方言 | mysql | doris（SQLGlot 已支持） |

**平台方式**：管理台注册 `doris://trove_ro:pass@doris-fe:9030/app_db`，然后 `kb/init`。

**本地方式**：

```bash
uv run trove --datasource doris://trove_ro:pass@127.0.0.1:9030/app_db
/kb init
```

> Doris 细节：FE 不支持 MySQL `COM_PING`，适配器用 `SELECT 1` 探活；`get_capabilities` 上报无事务、支持 CTE/窗口函数。生成/校验/执行全链路走 `doris` 方言。

## 4. 怎么保证查询准确（必须做对的三件事）

准确性不是数据源类型决定的，而是**知识库质量 + 自校验闭环**决定的。MySQL/Doris 一视同仁：

**① `/kb init` 是根基——不要跳过**
它产出 `.trove/kb/<database>/` 下：`schema_notes.yml`（表/列注释，LLM 起草 + 枚举探测合并）、`semantics.yml`（OSSIE 语义模型，metrics 带表限定表达式）、`examples.yml`（确定性模板 + 合成示例）、`lessons.yml`（Hint Bank）。语义模型是可答边界：查不到语义 = 拒绝并反问扩展模型，**杜绝瞎编**。

**② 校准语义模型（准确性最大杠杆）**
`semantics.yml` 里的 term/metric 是 SQL 生成的主视角。把业务口径（如「营收 = 净收入」「看日均用 30 日均值」）以 term→表达式写进语义层，准确性直接升档。改完 `/kb reload` 即时生效。

**③ 用 `/kb learn` 沉淀正确问答**
一轮回答正确后 `/kb learn` → 管理端确认 → 落库为参考 SQL。多轮累积后命中的问题走 KB 精确快径（word overlap ≥0.95 直接复用标准 SQL），不消耗 LLM 修正。错误答案自动蒸馏成 pending lesson，确认后进 Hint Bank 反哺生成。

**自带的自校验闭环（无需配置）：**
- 确定性快径（简单问题不烧 LLM）
- SQLGlot 方言校验 + 只读 AST 防火墙
- agentic gen_sql：`probe_query`（只读探测）→ `check_result`（规则链验证）→ 自我修正
- `reflect` 反思裁决 + 失败回滚/换方案
- 修正轮版本链回归硬检查

**Doris 专项建议：** 给只读账号开 `information_schema` 访问（`/kb init` 的枚举探测/列统计走它）；大宽表建议在 `schema_notes.yml` 里写明每个 metric 的正确聚合口径，减少复杂 SQL 修正轮数。

## 5. 权限与安全

应用层不是安全边界。建只读账号：

```sql
-- MySQL（固定来源 IP）
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';

-- Doris（同 MySQL 语法，或走 GRANT 管理面）
```

另建议 `MAX_EXECUTION_TIME`（MySQL）/ `query_timeout`（Doris）限制耗时；隐藏敏感列用列级授权或视图。所有执行的工具调用落 `sql_audit` 审计日志。

## 6. 需要 release 包吗？

**不需要。** Trove 不是安装即用的独立二进制：

- **代码交付**：`uv run trove` / `uv run trove serve` 即跑，Python >= 3.12。
- **平台交付**：`docker compose build` 生成 `trove-backend` / `trove-frontend` 两个镜像（Dockerfile 已具备多阶段构建），CI 每轮验证 `docker compose build`。
- **包形态**：`pyproject.toml` 支持 `uv build` 出 wheel/sdist，用于内网 pip 分发或集成到自己系统时可选；日常开发/部署用源码 + compose 即可，无需发布制品。
- 没有 LLM 凭证时产品功能受限（KB init 退化为纯骨架），这是使用前提而非 release 缺失。
