<div align="center">

# Trove

**问数 · 分析 · 决策 · 行动，一条对话链路。**

*开源、自托管的**数据决策智能体**:自然语言进,验证过的答案出;同一份人工确认的语义模型,既划定可答边界,也支撑零 LLM、带证据、知不确定性的定时判定——以及判定触发后行动的效果验收。*

[English](README.en.md) · [简体中文](README.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-8000%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

<br>

<a href="https://nivane.github.io/trove/"><img src="assets/banner.png" alt="Trove —— 数据决策智能体：自然语言进，验证过的答案出。一次提问穿过查询管线（意图路由 → 语义绑定 → 语义门禁 → 计划与编译 → 生成与共识 → 执行前三门 → 校验与脱敏 → 反思 → 交付），右侧是真实问答界面与分析过程面板" width="880"></a>

<br>

📖 **[文档站](https://nivane.github.io/trove/)** —— 46 页,每条机制都锚到具体源码行 · [用户 / 管理台图文指南](https://nivane.github.io/trove/user/ui-tour.html) · [接入数据源指南](https://nivane.github.io/trove/guide/datasource.html)

</div>

---

## 它是什么

Trove 是一个**自学习型数据决策智能体**:用自然语言提问,得到由真实 SQL 支撑的 Markdown 答案——执行前与执行后都被验证;当答案只能是猜测时,它会拒绝;同一份语义模型还会被定时求值成带证据、知不确定性的判定,触发即送达——行动发生在你的流程里(系统对业务数据保持只读),行动之后还有效果验收;每一次提问都会让它变得更好。

它承诺的不是「永远正确」,而是 **「错了也绝不轻易认输」**:

- **语义层划定边界。** 一份人工确认的语义模型(`semantics.yml`,Apache OSSIE 语义模型)是**唯一可答范围**——业务方已声明的 dataset / metric / field / relationship,仅此而已。模型之外的提问会被拒绝,并附一条一键扩展模型的路径,绝不靠猜裸表作答。
- **确定性护栏闭环。** 生成的 SQL 要过零 LLM 规则链、AST 防火墙、执行代价守卫,以及带 SQL 版本回归的反思循环。错的答案会被诊断、回滚、纠正,修正本身也被记住。
- **决策与问数同源,且知道自己有多确定。** 阈值规则用语义模型自己的词汇声明指标、窗口与基期,由确定性引擎求值,零 LLM;每次触发都带着它据以判定的 SQL 与原始行。判定还带一层统计诚实:季节基线做噪声带,样本不足 8 块不予确认,置信度明标「位置分数,非概率」——测不出就说测不出,绝不把「判不了」读成「今天没事」。
- **分析先统计,后叙事。** 「为什么跌了?」沿指标定义逐层拆解:贡献分解、比率归因与残差如实呈现;显著性、置信与季节基线出自一套纯 stdlib 的统计器械——可复算、可种子重放,结论永远能被重新算出来。数字全部来自确定性引擎,只有叙事用 LLM。
- **行动之后有效果验收。** 提案批准、外送不是终点:系统按约定窗口回来测量,把行动前后的数据与判定用的**同一套噪声带**比对,给出「超出噪声带 / 无可辨识变化」的三态结局——没有变化也是诚实的结论;测量与判定复用同一份解析口径,谁也无法悄悄换尺子。
- **学习沉淀为资产。** 每一次纠正蒸馏成一条 lesson,每一条被确认的问答成为参考 SQL。自动学习的内容一律以 `pending` 落地,直到管理员确认——知识在增长,同时可审查、可 git 管理、属于你。

## 全貌

```mermaid
flowchart TB
    %% 配色为站点同款的低透明度填充 + 实色描边，亮暗两档同一份源码都成立
    subgraph entry["入口"]
        UI["Web UI"]:::entry
        SRV["HTTP 服务"]:::entry
        CLI["CLI / REPL"]:::entry
        MCP["MCP"]:::entry
    end

    WF["编排 · trove/workflow<br/>LangGraph 工作流<br/>语义门禁 → 计划与编译<br/>生成 → 执行与校验 → 反思"]:::core

    subgraph caps["能力 · trove/services"]
        SEM["语义模型"]:::cap
        ANA["分析 · 归因分解 · 统计器械"]:::ana
        KB["知识库 + 混合检索"]:::cap
        MEM["记忆 · 判定规则 · Skills"]:::cap
        ACT["行动 · 提案 / 审批 / 回执 / 效果验收"]:::act
    end

    LLM["模型 · trove/llm<br/>LLM 网关"]:::base
    DS["数据源<br/>PostgreSQL · MySQL · Doris<br/>ClickHouse · DuckDB · SQLite<br/>Snowflake · BigQuery"]:::ext
    STATE["状态 · trove/storage<br/>PostgreSQL / SQLite<br/>会话 · 任务 · 检查点<br/>查询日志 · 谱系"]:::base

    UI --> WF
    SRV --> WF
    CLI --> WF
    MCP --> WF
    WF --> SEM
    WF --> ANA
    WF --> KB
    WF --> MEM
    MEM -->|"判定触发"| ACT
    WF -.-> LLM
    KB --> DS
    MEM --> STATE
    classDef entry fill:#71717a14,stroke:#71717a59,stroke-width:1px;
    classDef core fill:#6366f11f,stroke:#6366f1,stroke-width:2px;
    classDef cap fill:#6366f114,stroke:#6366f199,stroke-width:1.5px;
    classDef ana fill:#14b8a614,stroke:#14b8a699,stroke-width:1.5px;
    classDef act fill:#8b5cf614,stroke:#8b5cf699,stroke-width:1.5px;
    classDef base fill:#71717a14,stroke:#71717a59,stroke-width:1.5px;
    classDef ext fill:transparent,stroke:#71717a80,stroke-width:1.5px,stroke-dasharray:4 4;
    style entry fill:#71717a0d,stroke:#71717a2e,stroke-width:1px
    style caps fill:#71717a0d,stroke:#71717a2e,stroke-width:1px
    linkStyle 8 stroke:#f59e0b,stroke-width:1.5px
```

两个带(入口、能力)夹着编排——它是一张 LangGraph 图,所以画成一个节点;再加外挂的模型层与状态层,就是它的形状。数据源在 Trove 之外,所以不在五层里;**行动的执行同理**——判定触发产出的提案、审批与回执在内,执行在外(webhook 推 / MCP 拉)。实线是「谁调谁」,虚线是「谁会用到大模型」——两条都不是数据流。往里每一步的细节见[系统架构](https://nivane.github.io/trove/architecture/overview.html)与[查询工作流](https://nivane.github.io/trove/architecture/workflow.html);产品功能怎么分块、对谁开放、边界在哪见[功能架构](https://nivane.github.io/trove/architecture/functional.html)。

### 一次提问在图上怎么走

```mermaid
flowchart TB
    %% 与站点架构页同一套阶段配色:路由/出口 zinc · 门禁 amber · 生成 indigo ·
    %% 执行与校验 teal · 失败分析 红;低透明度填充 + 实色描边,亮暗两档同源
    Q(["提问"]):::entry --> ROUTE["route_intent 意图分流<br/>数据问题走主链,元数据走自校验链"]:::entry
    ROUTE --> SL["schema_linking 语义绑定<br/>把问题锚到语义模型上"]:::entry
    SL --> GATE{"语义门禁"}:::gate
    GATE -->|"结构上够不着"| REFUSE["refuse 拒绝<br/>+ 起草模型扩展,确认后重答"]:::gate
    GATE -->|"覆盖 / 部分覆盖"| FAST["fast_match 确定性快径<br/>KB 精确命中就直接给 SQL"]:::gate
    FAST -->|"未命中"| GEN["规划 → 编译 → 生成<br/>agentic 循环,可多候选投票"]:::gen
    FAST -->|"命中"| EXEC["执行前三道门<br/>授权 → 人在环 → 只读三层"]:::exec
    GEN --> EXEC
    EXEC --> VAL["执行 → 规则链校验 → 脱敏<br/>结果逐条核验后才算数"]:::exec
    VAL --> RE{"reflect 反思"}:::gate
    RE -->|"未过"| RB["analyze_error 失败分析<br/>版本比对 → 回滚重试"]:::err
    RB --> GEN
    RE -->|"通过"| OUT(["交付<br/>结论 · 图表 · 洞察<br/>归因 · 来源"]):::entry
    classDef entry fill:#71717a14,stroke:#71717a59,stroke-width:1px;
    classDef gate fill:#f59e0b14,stroke:#f59e0b99,stroke-width:1.5px;
    classDef gen fill:#6366f114,stroke:#6366f199,stroke-width:1.5px;
    classDef exec fill:#14b8a614,stroke:#14b8a699,stroke-width:1.5px;
    classDef err fill:#dc262614,stroke:#dc262699,stroke-width:1.5px;
    linkStyle 11 stroke:#8b5cf6,stroke-width:1.5px
```

28 个节点的完整版本、分支与回滚阶梯、每个节点的输入输出,见[查询工作流](https://nivane.github.io/trove/architecture/workflow.html)。

管线的真实样片——提问后右侧实时展开分析步骤,最终给出带柱状图、KB 命中与校验标记的答案:

<p align="center"><img src="assets/demo.gif" alt="Trove 演示：提问后右侧实时展开分析步骤（意图路由 → 生成 SQL → 校验 → 洞察 → 图表），最终给出带柱状图、KB 命中与校验标记的答案" width="880"></p>

## 分析 · 决策 · 行动

「数据决策智能体」的后三段在一条链上:**分析**把「为什么变了」拆开算清,**决策**按声明好的阈值定期回答「该不该警觉」,**行动**把触发冻结成一份等人拍板的提案,并在事后验收效果。三段与问数共用同一套纪律——**数字由确定性引擎算出来,LLM 只写解读**,每个数字都带可复算的证据。

**分析:「为什么变了」。** 归因问题(「为什么…」「什么导致…」)不走生成 SQL,走确定性分解:先按贡献最大的维度拆总变化(贡献分解 / 比率 shift-share),再沿指标表达式下钻成驱动器树。显著性交给统计器械:稳健 z(中位数 + MAD)对照历史同粒度块的噪声带——**超出噪声带 / 落在带内 / 判不了**,三态如实;算不出就返回空,绝不用 0 冒充「没变化」。整层纯 stdlib:不输出 p 值(小样本上是伪精度),种子一律确定性派生。实拍:

<img src="docs/assets/shots/user-chat-attribution.png" alt="Trove 归因分析卡:已声明度量 max_loan_amount 与「归因 · 分解」标记;总变化 +590,820 与主维度 loan_status;分类瀑布图与维度贡献表(C / D / B / A 的基期、当前、Δ 与贡献占比);底部证据入口「查询 2」" width="880">

**决策:「该不该警觉」。** 阈值规则用语义模型自己的词汇声明(指标 / 维度 / 过滤 / 时间窗),定时求值,判定过程零 LLM;「变了多少才算警觉」由季节噪声带回答——判不了是错误运行,绝不静默放行。触发时判定带着它当时判的 SQL 与原始结果行;条件满足时升级因果对照(placebo DiD → 无优化器合成对照),每条结论带假设清单。规则文件每次改动都是一次 git 提交,判定史按规则版本分桶回评;what-if 拿当时那份数字喂回同一套判定内核,重放「若 A 变 X,是否触发」而不重新查库。实拍:

<img src="docs/assets/shots/admin-verdict-history.png" alt="Trove 管理台 · 判定历史抽屉:每条判定带级别与一句话消息;展开的一条显示证据 SQL(SELECT SUM(loan.amount) FROM loan)、原始结果行 470,000 与判定行(当期 470,000 / 基期 100,000 / Δ 370,000 / Δ% 370.0% / 已触发);相邻判定用芯片标出关系(与上次相同 / 规则已修改 / 无前次可比);左侧判定质量卡按规则版本分桶(56da39b9 历史版本 / ae5a40ca 当前版本)" width="880">

**行动:「接下来做什么」。** 判定触发冻结成行动提案——**载荷在创建那一刻定死**,批准外送的就是看的那一份;模板先过 pending → 确认门,幂等键防重,风险上限与通道限速内建。人工审批通过才外送(webhook / IM),回执逐条落库;行动之后用**与判定同一套噪声带**复测「数字动了没有」(超出 / 无变化 / 判不了),并汇入判定质量回评。系统自身没有写回业务库的通道——`ActionService` 的构造签名里没有连接器,只读姿态由测试钉死。实拍:

<img src="docs/assets/shots/admin-actions.png" alt="Trove 管理台「行动」页的提案页签:页头写明「行动只提议、不触碰业务库 —— 批准之前,任何东西都不会离开系统」;待办计数(未闭环 1 · 待批准 1);一条待批准提案 loan-high → loan-baseline-alert(风险中,demo 数据源)与行尾的批准 / 拒绝 / 详情" width="880">

三段在一条链上闭合——判定跑在语义模型上,行动跑在判定上,验收跑在行动上:

```mermaid
flowchart TB
    M["语义模型<br/>指标 / 维度 / 时间窗"] --> R["判定规则<br/>零 LLM 定期求值"]
    R --> BAND{"季节噪声带<br/>超出才触发"}
    BAND -->|"判不了"| ERR["如实报错误运行<br/>绝不静默放行"]
    BAND -->|"超出 + 条件命中"| V["判定触发<br/>SQL + 原始行证据"]
    V --> C["因果对照(可选)<br/>DiD → 合成对照"]
    V --> P["行动提案<br/>载荷创建时冻结"]
    P --> A{"人工审批"}
    A -->|"批准"| D["外送 webhook / IM<br/>回执落库"]
    A -->|"拒绝"| X["终止"]
    D --> M2["效果验收<br/>同一套噪声带"]
    M2 -.->|"回评"| Q["判定质量<br/>按规则版本分桶"]
```

逐条展开:[判定规则](https://nivane.github.io/trove/capabilities/decisions.html) · [行动与审批](https://nivane.github.io/trove/capabilities/actions.html) · [分解数学与驱动器树](https://nivane.github.io/trove/engineering/decomposition.html) · [统计与噪声带](https://nivane.github.io/trove/engineering/statistics.html) · [闭环与契约](https://nivane.github.io/trove/engineering/closed-loop.html)。

## 为什么不是又一个 NL2SQL

裸 LLM agent 依据原始 DDL 写 SQL,对业务含义的错误自信是常态:名为 `A11` 的列、状态码 `A`,离开业务词典毫无意义,「平均贷款金额」也无法从 schema 推导。RAG 有帮助——词表、示例与教训能锚定生成——但它只负责**供弹药,不负责画边界**:检索漏掉时,它照样用貌似合理的猜测作答。

Trove 走语义层路线,并加了一个让答案持续流动的变体:

- **模型覆盖到了,就编译。** 查询计划编译到已声明的 metric / field / relationship 上——SQL 是权威产物,不是建议稿。
- **只缺词表、值或口径,就编一半。** join、过滤、分组被权威编译,生成 agent 只补没覆盖到的部分,执行前还有骨架保真校验兜底——答案照常交付,而不是硬停。
- **模型结构上够不着,就拒绝。** 未声明的表、二义的 join、fan-out——Trove 拒绝,并草拟一份模型扩展供你一键确认。**拒绝本身就是建模信号**,覆盖率随使用增长。

```mermaid
flowchart TB
    PLAN["查询计划"] --> C["语义编译器"]
    C -->|"全部命中"| OK["权威 SQL<br/>编译器全权产出<br/>无 LLM 补缺"]
    C -->|"软 MISS"| PC["词表 / 取值 / 口径未声明<br/>join、过滤、分组已钉住<br/>生成补缺 + 骨架校验兜底"]
    C -->|"硬 MISS"| MISS["未覆盖的表 / 二义 join<br/>fan-out / 坏派生定义<br/>拒绝 + 一键确认模型扩展"]
```

同一份编译器,三种结局,而且**中间的软 MISS 才是常态**——它把「模型没覆盖全」从一次硬停,变成一次照常交付。

人类真正在意的一切——哪个口径算数、哪些 join 合法、枚举值是什么意思、这件事超过多少算异常——在 YAML 里声明一次,对每个问题强制执行;而不是每个问题都重新猜一遍。

| | 裸 LLM agent | 仅 RAG 的 NL2SQL | 纯语义层 | **Trove** |
|---|:---:|:---:|:---:|:---:|
| 划定可答边界(语义模型) | ❌ | ❌ | ✅ | ✅ |
| 覆盖外拒绝,而非猜测 | ❌ | ❌ | 部分 | ✅ + 一键模型扩展 |
| 模型没覆盖全时仍能作答 | ❌ | ✅(靠猜) | ❌ | ✅ 编译骨架 + 生成补缺 |
| 执行前零 LLM 验证 | ❌ | ❌ | 部分 | ✅ 规则链 + AST 防火墙 + EXPLAIN 守卫 |
| 执行后自纠(反思 + 回归) | ❌ | ❌ | ❌ | ✅ 诊断回滚重试 + 版本比对 |
| 结论 → 阈值判定(零 LLM) | ❌ | ❌ | 部分(够不着语义层的基期与口径) | ✅ 语义模型词汇声明 + 证据留痕 |
| 判定带不确定性(季节噪声带 / 样本不足不硬判) | ❌ | ❌ | ❌ | ✅ 纯 stdlib 统计器械,置信如实标注 |
| 行动后效果验收(闭环回测) | ❌ | ❌ | ❌ | ✅ 与判定共用同一套噪声带 |
| 按数据源学习;自动内容需人审 | ❌ | 部分 | 部分 | ✅ KB + 记忆,`pending` 至确认 |

## 工程取舍

上表每一项背后是具体的算法、边界与 trade-off,各写过一篇深潜——骨架一致:解决什么问题 → 原理与算法 → 怎么用 → 放弃了什么 → 优势在哪:

| 主题 | 一句话取舍 | 深潜 |
|---|---|---|
| 语义编译边界 | 能编译的编译、缺词表的编一半、真错的拒绝并顺势把模型长大 | [语义编译边界](https://nivane.github.io/trove/engineering/semantic-compiler.html) |
| 分解数学与驱动器树 | 恒等式能证的才声称;乘除链宁可不拆,残差如实入证据 | [分解数学与驱动器树](https://nivane.github.io/trove/engineering/decomposition.html) |
| 统计与噪声带 | 「变了多少才算变了」用稳健 z 与诚实三态回答,不做 p 值表演 | [统计与噪声带](https://nivane.github.io/trove/engineering/statistics.html) |
| 判定内核 | 阈值声明在语义模型自己的词汇里,零 LLM 判定,证据可复算 | [判定内核](https://nivane.github.io/trove/engineering/decision-engine.html) |
| 因果与模拟 | 分解是描述,因果主张要过实验设计的升级梯;算不出就直说 | [因果与模拟](https://nivane.github.io/trove/engineering/causal-whatif.html) |
| 闭环与契约 | 判定 → 行动 → 验收走完整环;测量纪律写进 verifier 契约 | [闭环与契约](https://nivane.github.io/trove/engineering/closed-loop.html) |
| 行动安全架构 | 只读姿态由签名与测试钉死;模板闭集渲染;失败永不静默 | [行动安全架构](https://nivane.github.io/trove/engineering/action-safety.html) |
| 扩展与治理 | 扩展点单一路径;信封能力推导、装前试跑 / 影响面回放、包导出导入;组织资产 pending → 确认 → 版本化 → 回滚,可颗粒停用 | [扩展与治理](https://nivane.github.io/trove/engineering/extension-governance.html) |

## 快速开始

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。先挑一条路:

| 你想要 | 走哪条 | 大概要多久 | 从这里开始 |
|---|---|---|---|
| 先看看它长什么样 | 内置 BIRD 金融 demo | 5 分钟 | 下面的命令 |
| 一份能登录、能管理的完整部署 | Docker Compose(前端 + 后端 + PostgreSQL) | 15 分钟 | [Docker](#docker) |
| 接自己的数据库 | 注册 URL → `/kb init` 建模 → 提问 | 半小时起 | [接入你自己的数据](#接入你自己的数据) |

```bash
uv sync                                          # 安装依赖
uv run trove --datasource demo                   # 内置 BIRD 金融 demo 的交互 REPL

# 单次 CLI:问题经 stdin 传入,JSON 输出
echo "Which region has the highest average loan amount?" | uv run trove-cli --datasource demo --print
```

demo 数据源是内置的,但对话仍需要 LLM 凭证(项目根 `.env` 或环境变量,如 `DEEPSEEK_API_KEY`)。没有凭证就不作答——不存在静默的「mock 答案」回退。

### 接入你自己的数据

1. **连接** —— 一个数据库 URL:`--datasource postgres://user:pass@host:5432/db`。
2. **建模** —— 运行 `/kb init`:它基于你的实时 schema 起草初始语义模型(datasets / fields / metrics)。没有模型就没有答案,它会礼貌地拒绝,直到你完成建模——这正是边界在工作。
3. **提问** —— 然后自然提问。当它拒绝时,确认它起草的模型扩展,即可一步扩大覆盖并重答。

服务端部署下,同一流程在管理端完成(数据源注册 → KB init → 草稿审批)。逐步指南(MySQL / Doris / PostgreSQL / ClickHouse / DuckDB,以及云仓 Snowflake / BigQuery,含管理台与本地 REPL 两条路径):[接入数据源](https://nivane.github.io/trove/guide/datasource.html)。

### Docker

前端(nginx 托管 SPA + `/v1` 反代)与后端(纯 JSON API)是相互独立的镜像,可各自重建:

```bash
docker compose up --build        # → http://localhost:8080/(后端 :8000 仅供调试)
docker compose build frontend    # 只重建前端镜像;build backend 只重建后端
docker compose down
```

本地默认登录 admin / `admin123`(生产由 `TROVE_ADMIN_PASSWORD` 控制)。compose 栈默认 PostgreSQL,已预载 BIRD demo 数据;真实对话需要 LLM 凭证,见 `docker-compose.yml` 里 `~/.trove/conf` 挂载那一行的注释。

## 能力速览

每一行的深入版本都在文档站,带源码锚点:

| 能力 | 一句话 | 深入 |
|---|---|---|
| 语义层 | 可答边界是一份可读、可 diff、可 git 回滚的文件 | [语义层](https://nivane.github.io/trove/capabilities/semantic.html) |
| 主题域 | 语义模型里的可选收窄:域内收敛锚定、超域显式拒绝;管理台可按用户授权到域 | [语义层 · 主题域](https://nivane.github.io/trove/capabilities/semantic.html#topics) |
| 自校验闭环 | 规则链 / AST 防火墙 / 代价守卫 / 带版本回归的反思回滚 | [查询工作流](https://nivane.github.io/trove/architecture/workflow.html) |
| 判定规则 | 告警也走语义模型:阈值、窗口、基期零 LLM 求值,触发带证据与相邻 diff;季节噪声带判显著性,条件满足时升级因果对照,可在当时的数字上 what-if 回放 | [判定规则](https://nivane.github.io/trove/capabilities/decisions.html) |
| 主动扫描 | 按计划扫描指标 × 维度找异常,超噪声带只落草稿——管理员确认才成为规则;LLM 只提假设,引擎负责验证 | [判定规则 · 主动扫描](https://nivane.github.io/trove/capabilities/decisions.html#scan) |
| 行动 | 判定触发冻结成提案 → 人工审批 → 外送回执 → 效果验收;退避重试与 dry-run 预演内建,系统自身不接写通道 | [行动与审批](https://nivane.github.io/trove/capabilities/actions.html) |
| 判定质量 | 历史判定按规则版本分桶回评:触发率、行动有效 / 无变化 / 测不了的条数;样本不足不报比率 | [判定规则 · 质量回评](https://nivane.github.io/trove/capabilities/decisions.html#quality) |
| 订阅 | 定时分析的结果按「每期 / 仅告警」投递到通知通道,失败留痕;任务可限定主题域 | [自动化与治理](https://nivane.github.io/trove/admin/automation.html) |
| 知识库 | `/kb init` 起草 + 确认过的问答成为参考 SQL + 漂移报警 | [知识库](https://nivane.github.io/trove/capabilities/kb.html) |
| 混合检索 | 关键词 + 向量两路召回,权重可调可评,零 LLM 评测脚本 | [混合检索](https://nivane.github.io/trove/capabilities/retrieval.html) |
| 记忆 | 跨会话 episode、自动提取的偏好、per user × datasource 画像 | [记忆](https://nivane.github.io/trove/capabilities/memory.html) |
| Skills | 组织级方法论:四档——`required` 注入 / `available` 按需 / `validator` 断结果 / guard 断 SQL;确认门禁,可颗粒停用 | [Skills](https://nivane.github.io/trove/capabilities/skills.html) |
| 分析 | 「为什么下降?」沿指标定义递归拆成驱动器树,贡献分解与残差如实呈现;显著性、置信与季节基线出自可复算的统计器械;数字来自确定性引擎,只有叙事用 LLM | [Agent 能力](https://nivane.github.io/trove/capabilities/agent.html) · [噪声带](https://nivane.github.io/trove/capabilities/decisions.html#significance) |
| 八种数据源 | SQLite / PostgreSQL / MySQL / Doris / ClickHouse / DuckDB / Snowflake / BigQuery,一套模式 | [数据能力](https://nivane.github.io/trove/capabilities/data.html) |
| 接口与治理 | Web UI、REST(`/v1`)、MCP、CLI;管理端审批、审计、可观测 | [API](https://nivane.github.io/trove/reference/api.html) · [MCP](https://nivane.github.io/trove/reference/mcp.html) · [CLI](https://nivane.github.io/trove/reference/cli.html) |

数据源按需装驱动:`uv sync --extra postgres|mysql|doris|clickhouse|duckdb`(SQLite 内置);云仓另装 `--extra snowflake|bigquery`。LLM 走 litellm 网关,OpenAI / DeepSeek / Anthropic / 任意兼容端点皆可,每节点可分档——强模型做反思裁决,便宜模型做规划与洞察。见 [配置参考](https://nivane.github.io/trove/reference/config.html)。

管理台(管理端)是同一套栈的另一半:数据源接入、知识库审议、语义模型与主题域、判定规则、行动提案审批、订阅与审计;用户可见的数据源与主题域按人授权。这是知识库的待审批队列——自动沉淀的教训与示例先进待审批,管理员确认后才进入检索:

<img src="docs/assets/shots/admin-kb-pending.png" alt="Trove 管理台 · 知识库:待审批队列里每条自动学到的教训与示例都带「确认 / 编辑后确认 / 拒绝」,确认后写入认证块(批准人 / 时间)并进入检索" width="880">

## 只读执行:边界在代码里

问数系统要落地,安全边界不能是提示词里的一句请求。下面这些**全是代码,不是模型行为**(完整清单见[安全边界](https://nivane.github.io/trove/ops/security.html)):

| 边界 | 在代码里是什么 |
| --- | --- |
| **只读防火墙** | SQL 过 AST 解析后判定:非查询语句、任何 DML/DDL、`SELECT INTO OUTFILE`、危险函数、元数据表一律拒绝,不靠关键字黑名单 |
| **表级 allowlist** | 闸设在**执行路径本身**(`execute` / `explain` 都过),所以从 MCP、语义查询 API 或任何旁路进来的 SQL 同样受限 |
| **执行代价护栏** | 执行前用 EXPLAIN 估算最重算子行数,超软限(默认 5000 万)打回生成节点补 `LIMIT`,超硬限直接拒绝,不烧一轮 LLM 重生成 |
| **字段级脱敏** | 按字段声明 `partial` / `hash` / `null`,在结果出库、任何面向模型的节点之前改写;失败方向取严——该脱敏而读不出 salt 或语义模型,拒绝这次查询,不降级放行 |
| **不可信内容隔离** | 数据库单元格内容(以及检索、探测结果)先过注入扫描,命中即替换为隔离占位符再进提示;人确认过的 KB 内容不在此列 |
| **限流、配额与审计** | 按用户令牌桶与每日配额,超限 429;每次执行记录 question / SQL / verdict / 行数 / 耗时 / 错误 |
| **行动只提案,不执行** | 判定触发只把行动冻结成提案(载荷创建即定稿、幂等键去重),批准后才外送;行动层**不接数据源连接器**——系统自身没有写回业务库的通道,批准、外送与回执各自独立留痕 |

**应用层不是安全边界**——请始终用专用只读账号连接:

```sql
-- PostgreSQL(覆盖未来对象):CREATE ROLE trove_ro LOGIN PASSWORD '...'; GRANT pg_read_all_data TO trove_ro;
-- MySQL(按库授权,固定来源 IP):CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...'; GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

同样建议:用列级授权或视图隐藏敏感列;设置 `statement_timeout` / `lock_timeout`(PG)或 `MAX_EXECUTION_TIME`(MySQL);在数据库层收紧行数上限。语义模型里的 `row_filter` 是**声明式过滤**——它让「不该答的行」不进 SQL,但不替代上面这些库侧边界。

## 失败时,一律倒向拒绝

上面每一条都还可以被问「那它坏掉的时候呢」。答案在代码里是统一的:**判定不出,就往安全的那一侧倒**,而不是放行。这些方向是刻意选的,不是缺省行为:

| 场景 | 失败时 | 结果 |
|---|---|---|
| 认证组件缺失 | 拒绝启动 | 宁可起不来,也不静默地不安全 |
| 人在环的载荷认不出 | 默认拒绝 | 不执行,交给人 |
| 脱敏读不出 salt 或语义模型 | 拒绝执行 | 不降级为明文交付 |
| 拿不到授权依据(`None`) | 拒绝一切 | 「忘了授权」不退化成放行 |
| 只读自检查不成 | 记「未验证」 | 绝不渲染成「安全」 |
| KB 漂移检查读不出来 | 非零退出码 | 不把「没查成」当「没问题」 |
| 置信度判定不出 | 倒向最保守的档 | 虚低好过虚高 |
| 噪声带测不出(块数不足 / 无历史) | 如实记「判不了」 | 要求噪声带兜底的规则宁可报错误运行,不静默放行 |

同样的取向也写在别处:健康检查区分 `unavailable` 与 `degraded`(而不是一个「挂了」),规则链第一条失败即止(而不是给一堆模糊提示),掩码失败清空结果集(而不是留一份可能泄漏的旧结果)。完整清单见[安全边界](https://nivane.github.io/trove/ops/security.html)与[可观测性](https://nivane.github.io/trove/ops/observability.html)。

## 文档地图

📖 **[nivane.github.io/trove](https://nivane.github.io/trove/)** —— 46 页,产品概念到 API 参考,含 34 张真实界面截图的图文指南

| | |
|---|---|
| **上手** | [快速上手](https://nivane.github.io/trove/guide/quickstart.html) · [产品概念](https://nivane.github.io/trove/guide/concepts.html) · [安装部署](https://nivane.github.io/trove/guide/deploy.html) · [接入数据源](https://nivane.github.io/trove/guide/datasource.html) |
| **图文指南** | [用户指南](https://nivane.github.io/trove/user/ui-tour.html) · [管理台指南](https://nivane.github.io/trove/admin/console.html) · [部署与运维](https://nivane.github.io/trove/ops/deployment.html) |
| **架构** | [系统架构](https://nivane.github.io/trove/architecture/overview.html) · [功能架构](https://nivane.github.io/trove/architecture/functional.html) · [查询工作流](https://nivane.github.io/trove/architecture/workflow.html) |
| **能力** | [数据能力](https://nivane.github.io/trove/capabilities/data.html) · [语义层](https://nivane.github.io/trove/capabilities/semantic.html) · [知识库](https://nivane.github.io/trove/capabilities/kb.html) · [混合检索](https://nivane.github.io/trove/capabilities/retrieval.html) · [判定规则](https://nivane.github.io/trove/capabilities/decisions.html) · [行动与审批](https://nivane.github.io/trove/capabilities/actions.html) · [Agent 能力](https://nivane.github.io/trove/capabilities/agent.html) · [记忆](https://nivane.github.io/trove/capabilities/memory.html) · [Skills](https://nivane.github.io/trove/capabilities/skills.html) · [LLM 网关](https://nivane.github.io/trove/capabilities/llm-gateway.html) |
| **运维** | [安全边界](https://nivane.github.io/trove/ops/security.html) · [运维管理](https://nivane.github.io/trove/ops/admin.html) · [可观测性](https://nivane.github.io/trove/ops/observability.html) · [漂移治理](https://nivane.github.io/trove/ops/drift.html) · [评测与回归门](https://nivane.github.io/trove/ops/eval.html) |
| **参考** | [配置](https://nivane.github.io/trove/reference/config.html) · [CLI](https://nivane.github.io/trove/reference/cli.html) · [API](https://nivane.github.io/trove/reference/api.html) · [MCP](https://nivane.github.io/trove/reference/mcp.html) |

主图在 LangGraph 上的节点构成、语义门禁的三条出口、规则链与回滚阶梯、上下文预算——都在[查询工作流](https://nivane.github.io/trove/architecture/workflow.html)里;`serve` 运行中的 REST 文档在 `/v1/docs`。

## 评测

用自己的数据、自己的语义模型、自己的口径复现准确率——这里不放任何现成数字。BIRD 金融 demo 的 schema 与官方导出一致,可直接跑全反思管线:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial [--limit 10]
```

逐题判定(每条带 `qid` 与消耗的 token)落 `.trove/eval/results.jsonl`。`offline_eval.py record` 用真凭证录一遍轨迹,`replay` 之后**零 LLM 调用**反复打分;`eval_gate.py` 是 CI 里的回归门——指标变差即退出码 1。同一道门也吃扩展资产的装前试跑与影响面回放(`trove validate --run` / `--impact`,同样零 LLM,`--json` 顶层 `metrics` 直喂)。详见[评测与回归门](https://nivane.github.io/trove/ops/eval.html)。

## 开发

```bash
uv run pytest                     # 全量 8000+ 测试,mocked LLM,零网络 / 零 key
uv run pytest tests/workflow/     # 只跑 LangGraph 图与节点
uv run pytest -m "not slow"       # 跳过慢测试
```

代码布局:`trove/workflow/`(图与节点)· `trove/services/`(数据源、KB、语义层、判定规则、记忆、技能)· `trove/llm/`(litellm 网关、agent 循环)· `trove/storage/`(统一存储:会话、审计、checkpoint)· `trove/agent/`(会话编排)· `trove/cli/`。更深的架构说明在 `CLAUDE.md`。

## FAQ

**这是 text-to-SQL 框架还是 BI 工具?** 都不完全是——Trove 是数据决策智能体:你提问,它规划、编译、执行、验证,再用 Markdown + 图表 + 可审计轨迹回答;同一份语义模型还会被定时求值成带证据的判定,触发即送达。它不打算成为仪表盘搭建器。

**它为什么拒绝问题?** 因为语义模型就是可答边界——这正是产品本身。一次拒绝意味着「模型未覆盖这个问题」,而且它随身带着一份草拟好的扩展,确认后即可扩大覆盖并重答。悄悄对裸表瞎猜,正是这套架构要消灭的失败模式。

**RAG 扮演什么角色?** 负责喂生成——词表、参考 SQL、lessons、情景记忆,以匹配到的数据集为锚做确定性检索。它决定 Trove **怎么写**覆盖内的 SQL;从不决定**能不能答**。

**需要向量数据库吗?** 不需要。内置 SQLite FTS5 即可开箱检索;PostgreSQL 下同一实例同时承载 pg_bm25 + pgvector 混合检索;情景记忆在配置了 embedder 时用向量,未配置则优雅回退词法匹配。

**schema 漂移了,语义模型怎么保持诚实?** 运行时守卫保证行为安全,漂移检查把声明与实时 schema 对比并在管理端报 `stale`,此时 `/kb init` 重建会保留人工审过的定义。关键是它**不把「没查成」当成「没问题」**:数据源连不上、KB 读不出来,一律记「未验证」并给出非零退出码。见[漂移治理](https://nivane.github.io/trove/ops/drift.html)。

## 参与贡献

欢迎 bug 报告、功能想法、文档与 pull request。请保持改动聚焦,提交前确保测试通过。较大的改动请先开 issue 讨论。

## License

Trove 以 [Apache License 2.0](LICENSE) 发布。
