<div align="center">

# Trove

**对话你的数据。它会回答——并在每一次提问中变聪明。**

*开源、自托管的对话式数据智能体:自然语言进,验证过的答案出——以一份人工确认的语义模型作为唯一可答边界。*

[English](README.en.md) · [简体中文](README.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-4800%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

<br>

<img src="docs/assets/hero-chat.png" width="880" alt="Trove 对话界面:一句自然语言提问,得到结论、图表,以及右侧 12 步的分析过程">

<sub><i>一次提问的完整链路——经过验证的 SQL、图表,以及每一步推理过程,全部可见。</i></sub>

<br>
<br>

📖 **[能力地图](https://nivane.github.io/trove/)** —— 六组数据侧能力 + 十二项 Agent 侧能力,每条机制都锚到具体源码行 · [接入数据源指南](https://nivane.github.io/trove/guide/datasource.html)

</div>

---

## Trove 是什么

Trove 是一个**自学习型对话式数据智能体**:用自然语言提问,得到由真实 SQL 支撑的 Markdown 答案——执行前与执行后都被验证;当答案只能是猜测时,它会拒绝;每一次提问都会让它变得更好。

它的承诺不是「永远正确」,而是**「错了也绝不轻易认输」**。四根支柱支撑这个承诺:

- **语义层划定边界。** 一份人工确认的语义模型(`semantics.yml`,Apache OSSIE 语义模型)是**唯一可答范围**:业务方已声明的 dataset/metric/field/relationship,仅此而已。模型之外的提问会被拒绝,并附带一条一键扩展模型的路径——绝不靠猜测裸表来回答。
- **确定性护栏闭环。** 生成的 SQL 要过零 LLM 规则链(形态/过滤/取值/排序)、AST 防火墙、执行代价守卫与带 SQL 版本回归的反思循环。错误的答案会被诊断、回滚、纠正,修正本身也被记住。
- **决策与问数同源。** 问数 → 分析 → 决策是一条链:阈值规则用语义模型自己的词汇声明指标、窗口与基期,由确定性引擎求值,零 LLM;每次触发都带着它据以判定的 SQL 与原始行。
- **学习沉淀为资产。** 每一次纠正都蒸馏成一条 lesson,每一条被确认的问答都成为 reference SQL,跨会话记忆能召回 episode 与用户偏好。自动学习的内容一律以 `pending` 状态落地,直到管理员确认——知识在增长,同时保持可审查、可 git 管理、属于你。

## 为什么语义优先?

裸 LLM agent 依据原始 DDL 写 SQL——对业务含义的错误自信是常态。名为 `A11` 的列、状态码 `A`,离开业务词典毫无意义;「平均贷款金额」也无法从 schema 推导。RAG 有帮助:词表、示例与教训能锚定生成。但 RAG 只负责**供弹药,不负责画边界**——检索漏掉时,它照样用貌似合理的猜测作答。

Trove 走语义层路线(Cortex Analyst 一派),并加了一个让答案持续流动的变体:

- **全覆盖 → 编译。** 查询计划被编译到已声明的 metric/field/relationship 上——SQL 是权威产物,不是建议稿。
- **部分覆盖 → 编译骨架,交给生成补齐。** 只缺词表、值或口径(软 MISS)时,join/过滤/分组被权威编译,生成 agent 只补未覆盖的部分——执行前还有骨架保真校验兜底。
- **结构性 MISS → 拒绝、扩展、重答。** 模型结构上无法覆盖(未声明表/join 二义/fan-out)时,Trove 拒绝——并草拟一份模型扩展供你一键确认。**拒绝本身就是建模信号**;覆盖率随使用增长。

人类真正在意的一切——哪个口径算数、哪些 join 合法、枚举值是什么意思、这件事超过多少算异常——在 YAML 里声明一次,对每个问题强制执行;而不是每个问题都重新猜一遍。

## 横向对比

| | 裸 LLM agent | 仅 RAG 的 NL2SQL | 纯语义层 | **Trove** |
|---|:---:|:---:|:---:|:---:|
| 从自然语言写 SQL | ✅(常错) | ✅ | ❌ | ✅ 受治理 |
| 划定可答边界(语义模型) | ❌ | ❌ | ✅ | ✅ |
| 覆盖外拒绝而非猜测 | ❌ | ❌ | 部分 | ✅ + 一键模型扩展 |
| 部分覆盖仍可作答(软 MISS 骨架) | ❌ | ✅(靠猜) | ❌ | ✅ 编译骨架 + 生成补缺 |
| 执行前零 LLM 验证 | ❌ | ❌ | 部分 | ✅ 规则链 + AST 防火墙 + EXPLAIN 守卫 |
| 执行后自纠(反思 + 回归) | ❌ | ❌ | ❌ | ✅ 诊断回滚重试 + 版本比对 |
| 结论 → 阈值判定(决策规则,零 LLM) | ❌ | ❌ | 部分(告警够不着语义层的基期与口径) | ✅ 语义模型词汇声明 + 证据留痕 |
| 按数据源学习;自动内容需人审 | ❌ | 部分 | 部分 | ✅ KB + 记忆,`pending` 至确认 |
| 数据在哪都能跑(6 种引擎,自托管) | ✅ | ✅ | 依厂商 | ✅ Apache-2.0 开源 |

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
        wf["LangGraph pipeline<br/>intent → linking → sketch → gen<br/>→ execute → reflect → attribution"]
        semantic["Semantic layer<br/>semantics.yml · compiler · row_filter"]
        kb["Knowledge base<br/>terms · examples · rules · lessons"]
        decision["Decision layer<br/>语义模型词汇的阈值规则"]
        memory["Memory<br/>episodes · preferences · profiles"]
        skills["Skills<br/>组织级方法论 · 两档注入 + validator 断言"]
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
    wf --> semantic & kb & memory & skills
    semantic --> yaml_src
    kb --> yaml_src
    decision --> yaml_src
    yaml_src --> rt
    rt --> kb
    wf -->|"read-only adapters"| sources
    semantic -->|"compile over"| sources
    decision -->|"只读求值"| sources
    admin --> yaml_src
    state_store --> wf
```

### 一次问答如何发生

主图是 LangGraph 上的 **28 个节点**(按默认配置;下图虚线框里的 `clarify` 是可选分支,需显式 `build_graphs(clarify=True)` 才挂上,不计入 28)。

```mermaid
flowchart TB
    Q["提问"] --> INTENT["route_intent"]
    INTENT -->|"问数据 / 为什么"| PD["parse_date<br/>相对时间解析"]
    INTENT -->|"问元数据"| MD["answer_metadata<br/>+ metadata_check 自校验"]
    INTENT -->|"闲聊 / 纠正 / 写操作"| ANS["answer_chitchat<br/>answer_correction · answer_reject"]
    INTENT -.->|"草稿待确认"| CD["confirm_draft"]

    PD --> LINK["schema_linking<br/>数据集锚定<br/>渲染 semantic_context"]
    LINK --> GATE{"语义门禁"}
    GATE -->|"无模型 / 零命中"| REFUSE["refuse<br/>先跑 /kb init"]
    GATE -->|"覆盖内"| FM{"fast_match<br/>KB 精确命中?"}

    FM -->|"命中"| SQL["确定性模板 SQL"]
    FM -->|"未命中"| SKETCH["query_sketch<br/>LLM 计划 → 类型化 IR"]

    SKETCH --> COMP{"SemanticCompiler"}
    COMP -->|"全覆盖"| SQL
    COMP -->|"软 MISS(词表/值/口径)"| SKEL["PartialCompile 骨架<br/>join · 过滤 · 分组权威"]
    COMP -->|"硬 MISS(结构性)"| REFUSE
    REFUSE -.->|"草稿确认后重入"| CD
    CD -.-> PD

    SQL --> GEN["gen_retrieve → gen_assemble → gen_generate<br/>检索 · 装配 · 生成(ReAct 工具环)"]
    SKEL --> GEN

    GEN --> SEM["semantics<br/>口径复核"]
    SEM --> HITL{"hitl<br/>执行前人工确认(可选)"}
    HITL --> EXEC["execute_sql<br/>AST 防火墙 · EXPLAIN 行数守卫<br/>骨架保真校验 · 行过滤注入"]
    EXEC --> SEL["select<br/>多候选共识投票"]

    SEL --> CHK{"validate<br/>零 LLM 规则链<br/>形态 · 过滤 · 取值 · 排序"}
    CHK -->|"违规"| FIX["analyze_error<br/>诊断 & 回滚"]
    FIX --> SKETCH
    CHK -->|"通过"| MASK["masking<br/>字段级脱敏(LLM 之前)"]
    MASK --> REFLECT["reflect 裁决<br/>+ SQL 版本回归"]
    REFLECT -->|"不通过"| FIX
    REFLECT --> ATTR{"为什么 / 根因?"}
    ATTR -->|"是"| DRILL["attribution<br/>多跳下钻<br/>贡献率 · 瀑布"]
    ATTR -->|"否"| OUT["insights → chart → conclusion → output"]
    DRILL --> OUT
```

### 从问数到决策(零 LLM)

问数与分析已经足够厚,决策不必是提示词里的一句请求。规则用**语义模型自己的词汇**声明主体(指标、维度、窗口、基期),引擎确定性地解析窗口与基期、编译成 SQL、把结果交给条件语言判定;整条链路没有 LLM 参与。

```mermaid
flowchart LR
    R["decisions.yml<br/>语义模型词汇声明"] --> LINT{"写盘前 lint"}
    LINT -->|"不通过"| REJ["拒绝入库"]
    LINT -->|"通过"| GIT["git 提交<br/>可审 · 可 diff · 可回滚"]
    GIT --> JOB["定时任务绑定 decision_rule"]
    JOB --> EVAL["DecisionService<br/>窗口/基期解析 → SQL → 条件 AST"]
    EVAL --> JUDGE{"条件判定<br/>三值逻辑"}
    JUDGE -->|"触发"| ALERT["告警 + 证据<br/>rule_digest · model_version<br/>SQL · 原始行"]
    JUDGE -->|"未触发"| OK["ok 记录(同样落盘)"]
```

- **规则用业务词汇写。** `subject` 直接引用语义模型里的指标与维度,基期可选环比(`prev_period`)/ 同比(`yoy`)/ 字面量 / 无;严重度 `info` / `warning` / `critical`,可按聚合值或逐维度判定,收敛方式 `any` / `all` / `top_k`。
- **条件语言是封闭的。** 手写词法 + 递归下降解析(不用 `eval`,规则是不可信输入),变量集固定(`current` / `baseline` / `delta` / `delta_pct` / `contribution` / `row_count` / `dim`),函数只有 `abs` / `min` / `max` / `pct_change`;名字写错是**解析期报错**,不是悄悄失效。
- **三值逻辑,不误报。** 基期缺失时 `not (x > y)` 求值为 Unknown 而不是真——少一个基期不会换来一条假警报。
- **失败必须响。** 规则求值不出来(窗口无法解析、指标未声明、数据源连不上)就是一次 error 运行,绝不降级成安静的「没问题」。
- **证据即审计。** 决策运行没有 LangGraph trace,于是证据本身就是记录:用了哪份语义模型(`model_version`)、哪一版规则(`rule_digest`)、跑的是哪两条 SQL、判定依据的原始行(最多 200 行)。
- **规则与知识同源。** 落在 `.trove/kb/<datasource>/decisions.yml`,和词表、示例、教训同一个目录、同一套 git 审计;管理端提供规则页与原始 YAML 编辑器,并显示「哪些定时任务引用了它」。

### 如何学习(并保持受治理)

```mermaid
flowchart LR
    RUN["每一轮提问<br/>question · SQL · verdict · corrections"] --> OBS["observe"]
    OBS --> EP["episodes<br/>事实性召回(跨会话)"]
    OBS --> DR["草拟参考示例<br/>(pending)"]
    OBS --> LS["草拟 lesson<br/>(pending)"]
    OBS --> PF["偏好候选<br/>(pending)"]
    REFUSE["拒绝 / 覆盖缺口"] --> XT["语义模型扩展草稿<br/>metric · field · relationship"]

    DR & LS & XT & PF --> GATE{"管理员确认"}
    GATE -->|"确认"| KB["知识库<br/>examples.yml · lessons.yml<br/>semantics.yml · decisions.yml"]
    GATE -->|"驳回"| DISCARD["丢弃"]
    EP --> MEM["记忆存储<br/>episodes · user facts"]
    KB --> RETR["确定性检索<br/>以命中的数据集为锚"]
    MEM --> RETR
    RETR --> GEN["下一轮生成"]

    SCHEMA["实时 schema"] --> DRIFT{"对比已声明语义"}
    DRIFT -->|"失配"| STALE["漂移报告 → 重建 / 重新 init<br/>保留人工审过的资产"]
    STALE --> KB
    DRIFT -->|"查不成"| UNKNOWN["记为「未验证」<br/>绝不当成「没问题」"]

    SKILLS["组织级方法论<br/>草稿 → 确认"] --> GEN
```

<p align="center">
<img src="docs/assets/hero-semantic.png" width="880" alt="Trove 语义层管理端:44 个指标、8 个数据集、56 个字段已声明,以及 10 条待审批">
</p>

<sub><i>治理面——模型声明了什么,以及还有什么等待人工确认。</i></sub>

## 能力清单

- **语义优先边界 + 分级作答** — 覆盖内全量编译;软 MISS 编译骨架、agent 补缺;结构性 MISS 拒绝 + 一键模型扩展重答。
- **确定性自校验** — 零 LLM 规则链(形态/过滤/取值/排序)、AST 防火墙(只读白名单、拦截 DML)、EXPLAIN 行数守卫、带 SQL 版本回归的反思循环。
- **快径与智能体双路径** — 简单问题走确定性 KB 模板快径;复杂问题升级到 agentic ReAct 循环(`validate_sql` / `probe_query` / `check_result` 工具,由模型自行判定完成);歧义问题多候选生成 + 投票。
- **决策规则层** — 用语义模型词汇写阈值规则(指标 / 维度 / 窗口 / 基期 / 严重度 / 收敛方式),零 LLM 求值;可挂定时任务,触发即带证据;规则写盘前 lint,落 KB 目录、进 git 审计。见上文「从问数到决策」。
- **为什么类问题的根因归因** — 「为什么营收下降?」得到多跳下钻:当前期 vs 基期、维度拆解,再下钻到最大贡献项。贡献率计算是确定性的(零 LLM),主拆维度**由数据决定**(Σ|Δ| 最大者,而非盲信 LLM 顺序);比率指标按 shift-share 分解为本征/结构/交叉三效应;只有叙事由 LLM 生成,且只允许引用归因表中的数字。
- **按数据源自学习的知识库** — `/kb init` 起草 schema 注释 + 语义模型 + 确定性术语与模板;确认过的问答成为 reference SQL;纠正蒸馏进 Hint Bank;漂移检测在模型落后于 schema 时报警。
- **语义即代码** — KB 的语义文件在每次写操作后自动 git 提交(`git log` 即审计历史,`git diff` / `git revert` 即评审与回滚),提交可带 `Generator` / `Approved-by` trailer;坏语义在写盘前就被拒绝,连提交这一关也拦得住。
- **声明式行级过滤** — 数据集可在语义模型里声明 `row_filter`(一条恒真布尔谓词),编译期注入到所有引用该数据集的查询顶层 WHERE;**快径与编译路径共用同一份注入实现**——所以「走快径就漏过滤」这类分叉不会发生。它约束的是「这个数据集能答什么」,不等于替代数据库侧的只读角色。
- **统一跨会话记忆** — episodic 召回、自动提取的用户偏好、per user × datasource 画像;自动内容一律 `pending` 至管理员确认。
- **组织级方法论技能** — 跨数据源的方法论(怎么规划、怎么诊断、组织口径)以 SKILL 形式管理,草稿同样需管理员确认。**注入两档**:`required` 正文进对应节点的系统提示,`available` 只在生成节点列出描述、由 `load_skill` 按需取用。**第三档 `validator` 不注入**——它拿组织自己的断言去查查询结果(聚合级、零 LLM,复用决策规则的表达式引擎),给**通过 / 违反 / 判不了**三值判定,`blocking` 档拦、`advisory` 档只作为附注进回答。判不了单独占一值:塌成通过是「没查却报平安」,塌成违反是「查不了却拦下正确结果」,两种都比不检查更坏。数据源事实归 KB,方法论归技能。
- **答案级置信度披露(只披露,不改行为)** — 回答里带一个分数,它由**档位基准 + 具名微调**算成:基准看答案来源(认证复用 / 复用 / 语义编译 / 生成四档,各占一段互不重叠的分数带,所以一个分数读得回它自己的档),微调是几条明写理由的加减(每个软 MISS 缺口 −0.05、agent 自检全过 +0.10、降级到经典子图 −0.10、检索证据强 +0.05)。**这些权重是判断,不是标定过的概率**——代码里也这么写着,谁都不许把它们说成校准值。它不进任何条件分支:把开关关掉跑一遍,SQL、行数、裁决、答案来源逐字节相同。
- **治理即特性** — YAML 是唯一真源(git 可审、可 diff);每次执行的工具调用都进审计;可选 HITL 人工确认;管理端提供 KB init、草稿审批、漂移报告、决策规则与技能管理。
- **可审计的分析轨迹** — Web UI 展示推理全过程:schema-linking 匹配、编译决策、规则链结果、agent 工具调用与修复原因。生成本身是三个带 checkpoint 的阶段——检索 → 上下文装配 → 生成——分步可观测、可独立测试,同时对 UI / CLI / 流式契约仍合并为单一 `gen_sql` 步。
- **会解释自己的错误** — 失败的一轮返回一张人话卡片(发生了什么、可以怎么办、重试是否有意义),不再把内部节点名与错误 slug 泄漏到界面上;原始诊断收进折叠区,仅管理员可见。
- **Web UI + REST API** — Vue SPA 对话(流式、表格、图表、HITL 对话框);一切在 `/v1` 之下,含声明式语义查询端点 `/v1/semantic/query`——直接把结构化计划编译到语义模型,不走对话管线。
- **MCP server** — 通过 stdio / SSE / streamable-http 把 NL→SQL 暴露为工具与资源,供 Claude Code 等 MCP 客户端使用。
- **多引擎、同一套模式** — SQLite / PostgreSQL / MySQL / Doris / ClickHouse / DuckDB 适配器 + 每数据源独立 KB;内部统一存储在 PG(生产)/ SQLite(测试回退);管理端 checkpoint 时间轴支持从任意节点续跑。
- **LLM 无关、双语统一** — litellm 网关(OpenAI / DeepSeek / Anthropic / 任意兼容端点),每节点模型分层(草稿用便宜模型、反思用强模型),统一 `zh` / `en` 交互。
- **可观测、可中断** — 真实依赖健康检查、Prometheus 指标、request-id + run-id 日志关联、每轮 token 成本落进会话历史(崩溃/重启后仍可查)、可选 Langfuse;取消查询会中断数据源驱动本身,而不只是挂起的协程。

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

想先看全貌再动手:[能力地图](https://nivane.github.io/trove/) 把每条能力锚到源码行,可以边读边核。

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
- `GET /v1/metrics` — Prometheus 文本格式:按路由/状态的 HTTP、按 provider/model/status 的 LLM 调用次数与耗时(`trove_llm_calls_total` / `trove_llm_call_duration_seconds`)、按数据源的 SQL 执行、in-flight 计数。
- `POST /v1/semantic/query` — 声明式语义查询 API:把结构化计划直接编译到语义模型,不经对话管线。
- 每个响应携带 `X-Request-ID`,并出现在该请求的每一行日志里。
- 客户端中止端到端取消:图任务被取消,适配器的驱动级中断被触发(sqlite3 / psycopg / MySQL `KILL QUERY` / duckdb)。

管理端点(`/v1/admin/*`,需 admin 角色或其令牌 scope):

- **数据源与 KB** — 注册(注册即连接探测,失败直接报原因)、`kb/init`、草稿审批、漂移报告。
- **决策规则** — `GET /v1/admin/decisions`(列表带 lint 问题与「被哪些任务引用」)、`PUT /v1/admin/decisions`(结构化规则或整份原始 YAML,写入前必过 lint 门禁)、`GET /v1/admin/decisions/raw`。
- **技能** — `GET/POST /v1/admin/skills` 及 `draft` / `llm-draft` / `confirm` / `reject` / `tier` / `body` 系列:组织级方法论的草稿、确认与分档。
- **定时任务** — `POST /v1/admin/jobs` 创建定时提问(cron / interval),可选阈值告警(`row_count >= 5` / `value > 1000` / `col:<name> <op> <n>` / `no_rows` / `verdict == <x>`)或绑定一条决策规则(`decision_rule`),通道 `console` 或 `webhook:<url>`,以及冷却分钟数;`GET/PATCH/DELETE /v1/admin/jobs/{id}`、`POST /v1/admin/jobs/{id}/run`(立即执行)、`GET /v1/admin/jobs/{id}/runs`(历史)。
- **记忆、用户与审计** — `GET /v1/admin/memory/profile`、偏好的确认与驳回、用户与数据源授权、审计日志(`GET /v1/admin/audit`)、会话 checkpoint 时间轴(可从任意节点 `replay_from` 重放)。

调度:**`serve` 内置调度 tick**,到点的任务自动执行(轮询间隔 `agent.scheduler_poll_seconds`,默认 30s)。不要在 `serve` 之外并行跑 `trove-cli schedule --daemon`(会重复执行)。绑定决策规则的任务走确定性引擎,**永不进入 NL 管线**——决策运行必须可复现、不依赖模型。

### MCP server

```bash
uv run trove mcp                                                            # stdio(默认)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

工具:`ask_data` · `list_datasources` · `kb_status`。资源(只读):`trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`。

非回环地址上监听却**不带** `--token`,服务会直接拒绝启动;带了 token 则每个 HTTP 请求都要过 `Authorization: Bearer`,并按该 token 所属用户的授权范围裁剪可见数据源。

### REPL 命令

| 分组 | 命令 |
|---|---|
| 会话 | `/help` `/exit` `/clear` `/compact` `/tasks` |
| 知识 | `/tables` `/table_schema` `/schemas` `/databases` `/kb …` `/init` `/facts` |
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

每个数据库在 `.trove/kb/<database>/` 下沉淀自己的知识库(schema 注释 / 语义模型 / 示例 / 规则 / 教训)。新增数据源:实现 `DatabaseAdapter` 方法并在 `registry.py` 注册,驱动按需懒加载。逐步接入指南(MySQL / Doris / PostgreSQL / ClickHouse / DuckDB,含管理台与本地 REPL 两条路径):[接入数据源](https://nivane.github.io/trove/guide/datasource.html)。

## 安全(只读执行)

问数系统要落地,安全边界不能是提示词里的一句请求。下面这些**全部是代码,不是模型行为**:

| 机制 | 做什么 |
|---|---|
| 只读语句防火墙 | 对 SQL 做 AST 解析后判定:非查询语句、任何 DML/DDL(含改写数据的 CTE)、`SELECT INTO OUTFILE`、危险函数(`SLEEP` / `LOAD_FILE` / `PG_READ_FILE`…)、元数据表(`sqlite_master` / `information_schema` / `pg_catalog`…)一律拒绝——不靠关键字黑名单 |
| 表级 allowlist | 按数据源配置允许访问的表;这道闸设在**执行路径本身**(`execute` / `explain` 都过),所以从 MCP、语义查询 API 或任何旁路进来的 SQL 同样受限,元数据表始终拒绝 |
| 执行代价护栏 | 执行前用 EXPLAIN 估算最重算子行数:超软限(默认 5000 万)打回生成节点补 `LIMIT` / 收窄过滤,超硬限(默认 10 亿)直接拒绝,不烧一轮 LLM 重生成 |
| 工具权限 | 工具按角色裁剪后才交给模型;越权调用在运行时折叠成「未知工具」,模型看到的是「没有这个工具」,而不是一道可以试探的墙 |
| 外部数据隔离 | 来自数据库单元格的内容(以及检索/探测结果)先过注入模式扫描,命中即替换为隔离占位符再进提示——人确认过的 KB 内容不在此列 |
| 字段级脱敏 | 语义模型里按字段声明 `partial` / `hash` / `null`,在**结果出库、任何面向模型的节点之前**改写。它在图上是独立节点而不是挂在 `select` 后面——快径命中时 `select` 直接返回,挂在里面的后置步根本不会执行;位置在规则链**之后**,先判原始数据对不对,再脱敏给人看。失败方向取严:该脱敏而 salt 或语义模型读不出来,一律拒绝这次查询,不降级放行 |
| 执行前人工确认 | 可选节点,SQL 执行前交人过目;载荷无法识别时默认拒绝,而不是放行 |
| 模型预算护栏 | ReAct 循环受轮次、墙钟时间、累计 token 三重约束,触顶即降级回经典子图,不会无限烧下去 |
| 凭据加密落盘 | 数据源配置里的密码 / token 等敏感字段以 Fernet 加密后存盘(`enc:v1:…`),密钥取环境变量 `TROVE_SECRET_KEY`,缺省则在配置旁生成 `0600` 的 `secret.key` |
| 限流与配额 | 按用户的内存令牌桶(默认 30 次/分)与每日配额(默认 300),超限返回 429 + `Retry-After`;管理端可热更新 |
| 令牌 scope 与默认拒绝 | 令牌可只授 `query` scope(能问数、不能管理);没有配置认证组件时 API 直接拒绝启动,而不是悄悄放行 |
| 查询审计 | 每次执行记录 question / SQL / verdict / 行数 / 耗时 / 错误,可从审计端点回查 |

**应用层不是安全边界**——请始终用专用只读账号连接:

```sql
-- PostgreSQL(覆盖未来对象)
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL(按库授权,固定来源 IP)
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

同样建议:用列级授权或视图隐藏敏感列;设置 `statement_timeout` / `lock_timeout`(PG)或 `MAX_EXECUTION_TIME`(MySQL);并在数据库层收紧行数上限。语义模型里的 `row_filter` 是**声明式过滤**,它让「不该答的行」不进 SQL,但不替代上面这些库侧边界。

## 配置

优先级:CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`:

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm 模型串
  model_fast: deepseek/deepseek-chat   # 便宜档:草稿、语义、洞察
  language: zh                         # 交互语言:zh / en
  # explain_row_guard: true            # EXPLAIN 行数守卫(默认开)
  # explain_max_rows: 50000000         # 超软限打回重生成
  # explain_hard_max_rows: 1000000000  # 超硬限直接拒绝
  # api_rate_per_minute: 30            # 按用户请求限流(0 = 关)
  # api_daily_quota: 300               # 每日配额(0 = 关)
  git_kb: true                         # KB 语义文件写操作自动 git 提交
  # confidence_score: true             # 答案级置信度披露(默认开;进程级,改了要重启)
  # attribution:                       # 为什么类问题的根因下钻(默认开)
  #   max_hops: 2                      # 1 = 仅维度拆解,2 = 再下钻最大贡献项
  # node_models:                       # 每节点模型覆盖(query_sketch / reflect / …)
  #   query_sketch: deepseek/deepseek-chat
  memory:
    enabled: true                      # 统一记忆子系统
    # promotion: false                 # 教训自动晋升(默认关)
```

API key 放项目根 `.env`(自动加载、已 gitignore)或导出环境变量(如 `DEEPSEEK_API_KEY`)。自定义 OpenAI 兼容端点配在 `agent.providers`;可选 Langfuse 追踪走 `agent.observability.tracing.enabled`;内部状态存储用 `TROVE_STORAGE_URL` 指向 PostgreSQL(不设则回落本地 SQLite)。

## 评测

用自己的数据、自己的语义模型、自己的口径复现准确率——Trove 自带 BIRD 金融 demo(schema 与官方 BIRD 导出一致)与全反思管线的评测脚本:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

逐题判定——每条带 `qid` 与消耗的 token——落 `.trove/eval/results.jsonl`,失败在 `failures.jsonl`;用 `scripts/distill_lessons.py` 批量蒸馏失败为 lessons。这里不放任何现成数字——请对着你自己的问题、你自己的语义模型、你自己的 schema 来量。

### 离线回放(录一次,之后免费打分)

`scripts/offline_eval.py record` 用真凭证跑一遍问题集并录下轨迹;`replay` **零 LLM 调用**打分——完成率/正确率/token 成本/失败恢复率——改提示词和规则时可以反复迭代而不必每次付费:

```bash
uv run python scripts/offline_eval.py record --questions qs.txt --output .trove/eval/replay.jsonl
uv run python scripts/offline_eval.py replay --input .trove/eval/replay.jsonl
```

### 回归门(零 LLM,默认开)

`scripts/eval_gate.py` 把基线结果文件与本次结果文件对比,**变差即退出码 1**:EX、编译命中率、完成率、恢复率、gold 精确匹配与 token 成本——每项各有方向和容差(`--tol ex=0.02`,或相对量 `--tol ex=0.10-r`),`--min-n` 拒绝样本不足,`--json` 供 CI 解析。它吃三类产物:`results.jsonl`(eval_bird)、`replay.jsonl`(离线回放)、以及检索/RRF 脚本 `--scorecard` 输出的 JSON。

可复现基线落在 `eval/baseline/`——固定问题集(32 题)+ 结果(32/32)+ **钉住的指标快照 `scorecard.json`**——按稳定 `qid` 对账,用 `scripts/build_eval_baseline.py` 生成或迁移、`scripts/eval_baseline.py check --require-full` 校验完整性与覆盖。

回归门**默认开**(`eval.gate_enabled: true`),`.github/workflows/eval-gate.yml` 每次 push/PR 触发,三层各挡一类问题:基线完整性(`--require-full`)→ 零 LLM 回放 → 门判定。零 LLM、零网络、零数据库。

门判定比较的是**两个时刻**:一边是仓库里钉住的 `scorecard.json`,一边是 CI 现算的。不拿基线跟自己比——`--baseline X --current X` 恒等 Δ=0,永远不会红,那是冒充回归门。

⚠️ **它挡的是「锚点坏了」(基线缺题、评分口径漂移、冻结产物被改),不是「模型变差了」**——后者要真库 + LLM,留在手动 `workflow_dispatch`,跑一轮 32 题约 170 万 token,请自行确认成本。

### 检索评测

检索质量有独立的零 LLM 脚本:`eval_retrieval.py`(召回 / 子结构覆盖 / 预算挤占,词法与 gold 表锚定两个口径)、`eval_hybrid_retrieval.py`(分路消融)、`eval_bird_retrieval.py`(BIRD 表级 schema 召回)、`tune_rrf.py`(RRF 权重网格)——后三者可输出 scorecard 交给回归门。

## 开发

```bash
uv run pytest                     # 全量(4800+ 测试,mocked LLM,零网络/零 key)
uv run pytest tests/workflow/     # LangGraph 图与节点
uv run pytest tests/services/kb/  # 知识库
uv run pytest -m "not slow"       # 跳过慢测试
```

CI(`.github/workflows/backend.yml`)跑同一套测试(非 integration 档,零网络 + PG integration 子集),外加 `ruff check` 与 `pip-audit` 依赖漏洞扫描;前端有自己的 workflow(`npm run ci` + 两个镜像的构建验证)。

零 LLM 运维脚本:`scripts/lint_kb.py`(KB 质量检查,可选实时枚举探测)、`scripts/check_drift.py`(声明语义 vs 实时 schema;退出码 0/1/2,且**「查不成」记 2 不记 0**——连不上库时的绿是假绿)、`scripts/check_kb_anti_cheat.py`(KB 模板若抄了 gold SQL 即失败)、`scripts/check_page_anchors.py`(校验[能力地图](https://nivane.github.io/trove/)里的源码锚点是否还指着同一段代码)。

代码布局:`trove/workflow/`(LangGraph 图、节点、确定性规则链)· `trove/services/`(数据源适配器、KB、语义层、决策规则、记忆、技能、SQL)· `trove/llm/`(litellm 网关、agent 循环)· `trove/storage/`(统一后端:会话、审计、checkpoint)· `trove/agent/`(会话编排)· `trove/cli/`(REPL 与命令)。

更深的架构说明在 `CLAUDE.md`;`serve` 运行中的 REST API 文档在 `/v1/docs`。

## FAQ

**这是 text-to-SQL 框架还是 BI 工具?** 都不完全是——Trove 是对话式数据智能体:你提问,它规划、编译、执行、验证,再用 Markdown + 图表 + 可审计轨迹回答。它不打算成为仪表盘搭建器。

**它为什么拒绝问题?** 因为语义模型就是可答边界——这正是产品本身。一次拒绝意味着「模型未覆盖这个问题」,而且它随身带着一份草拟好的扩展,确认后即可扩大覆盖并重答。悄悄对裸表瞎猜,正是这套架构要消灭的失败模式。

**决策规则和普通告警有什么区别?** 普通告警只能写「某列 > 某个数」,够不着语义层里的基期、口径与比率。Trove 的规则直接用语义模型的指标与维度声明主体,窗口与基期(环比 / 同比 / 字面量)由引擎确定性解析,条件在封闭语言里求值(名字写错是解析错误,不是静默失效),并且每次触发都留下它据以判定的 SQL 与原始行。规则与知识库同目录、同 git 审计。

**RAG 扮演什么角色?** RAG 负责喂生成——词表、reference SQL、lessons、情景记忆,以匹配到的数据集为锚做确定性检索。它决定 Trove **怎么写**覆盖内的 SQL;从不决定**能不能答**。

**需要向量数据库吗?** 不需要。开箱即用内置 SQLite FTS5 镜像检索;PostgreSQL 下同一存储同时承载 pg_bm25 + pgvector 混合检索;情景记忆在配置了 embedder 时可用向量,未配置时优雅回退词法匹配。

**需要什么 LLM?** 任意 litellm 兼容模型。质量建议:强模型做反思/裁决,便宜模型做规划与洞察(`conf/agent.yml` 每节点分档)。注意:demo REPL 没有 LLM 凭证就不作答——不存在静默的「mock 答案」回退。

**schema 漂移了,语义模型怎么保持诚实?** 运行时守卫(编译 guardrail、表存在性校验、声明式行过滤)保证行为安全;漂移检查把声明与实时 schema 对比,在管理端报 `stale`,此时用 `/kb init` 重建会保留人工审过的定义;同一检查也能无头运行(`scripts/check_drift.py`,零 LLM,退出码 0/1/2)挂 cron 或 CI。关键在它**不把「没查成」当成「没问题」**:数据源连不上、KB 读不出来,一律记「未验证」并给出非零退出码——门禁不会在库不可达时最绿。

## 路标

正在推进的八个方向与判据写在[能力地图的路标一节](https://nivane.github.io/trove/#road)——每条都给出「做到什么程度算做到了」,而不是日期。

**八条目前都还没有走完。** 逐条现状(不排名次):

| 方向 | 现状 |
|---|---|
| 01 语义漂移治理 | L1 结构层与 L2 引用层检测都在跑,区分「没漂移」与「没查成」,后台周期检测落进漂移库(与 `/v1/admin/drift` 同一份文件);缺的是门禁脚本还没进 CI |
| 02 执行画像 | 执行证据的形状与三态语义已定;但扫描量这一格六个方言都还没读,`scanned_rows` 恒为空 |
| 03 已验证查询资产 | 治理维度(`status` / `owner` / `approved_by` / `approved_at` / `source`)随确认逐条写入 `examples.yml`,`certified` 必须有人(I5),认证门不过整批拒绝——「每条资产可追溯到它的确认人」已成立;缺的是台账:`AssetLedger`(`runs` / `p50_ms`)已实现、有测试钉住,但**没有任何生产构造点**,且 `status` 还没参与检索加权 |
| 04 可核验闭环 | 证据链第一段(执行证据)已落地;「任意一条结论走回原始数据」还走不通 |
| 05 语义分支评审 | 尚未开工 |
| 06 身份鉴权与字段脱敏 | 执行前的表级授权门、LLM 前的字段级脱敏、以用户身份重放、审计与指标都已落地;CTE 名误判成表的解析缺陷已修,`warn` 命中的遥测出口已补(计数器给量、审计行给表名);缺的是表级门默认仍是 `warn`——切档要等观察期的误伤数据,那要真部署才收得到 |
| 07 成本归因与预算 | 记账已覆盖主路径;全部 32 个模型调用点中仍有不产生记录的 |
| 08 不可信数据输入边界 | 两条通道与隔离核已落地并被测试钉住;组织级 skill 正文此前以字符串拼接连入 system prompt、绕开两条通道,现已收成单一门(围栏 + 来源标注,两档同策略),确认关口另扫一遍并把命中报给管理员;清单枚举对「字符串怎么拼」的盲区记在那份测试的文档串里 |

把这份清单摆在 README 里而不是只说「已完成三条」,是因为路标的价值在于它能不能被用来排期:一条写着「达成」而实际没达成的判据,比一条写着「未达成」的判据有害得多。

## 参与贡献

欢迎 bug 报告、功能想法、文档与 pull request。请保持改动聚焦,提交前确保测试通过。活跃开发中的架构与设计文档随团队维护——较大的改动请先开 issue 讨论。

## License

Trove 以 [Apache License 2.0](LICENSE) 发布。
