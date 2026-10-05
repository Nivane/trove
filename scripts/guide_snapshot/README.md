# 种子快照（文档截图实例）

`frontend/scripts/capture-guide-shots.mjs`（34 张用户/管理指南截图）所拍摄的隔离演示实例的**冻结状态**。
由 `scripts/seed_guide_instance.py --snapshot-out <dir>` 产出；这里是产物本身。

## 这里有什么

| 路径 | 内容 |
|---|---|
| `root/.trove/kb/demo/`、`root/.trove/kb/financial/` | 两个已注册数据源的 KB YAML（语义即代码，含播种产生的 pending 示例/教训与 `decisions.yml` 的 action 接线）。检索镜像 `kb.sqlite` 是派生件，不在此处——首次打开会从这些 YAML 重建。 |
| `seed.json` | 产出快照那次播种的状态日志（任务 id 与计数）。 |

## 不在这里的（以及为什么）

- **会话/记忆库**（`home/sessions.sqlite`、`user_facts.db`、`memory/*.sqlite`）：
  实例私有状态，不入库。**没有它们，全新实例上第 3 步会真问 4 个问题（LLM）**；
  `--snapshot-out` 会把它们写进你的本地快照目录，带着它们走即可零 LLM。
- **`app.db` / `settings.db` / 任务 / 判定 / 行动各库**：种子脚本零 LLM 确定性重建。
- **`secret.key` / `datasources.yml`**：实例各自的密钥与加密凭据；脚本用实例自己的 key 重新注册数据源。

## 恢复到全新实例

前置：本机 MySQL BIRD 导出在 `127.0.0.1:3306`（`root:root`，库 `financial`）——financial
数据源的会话与 KB 都在它上面。

```bash
# 1. 建实例目录：复制 scripts/guide_instance.example.yml 到实例里，改掉 home 路径
# 2. 后端**停止**状态下套用快照：
uv run python scripts/seed_guide_instance.py --restore scripts/guide_snapshot \
    --project-root <实例目录> --home <实例目录>/home
# 3. 从实例目录启动后端：
cd <实例目录> && uv run --project <仓库> trove serve --port 8000 --config <实例目录>/agent.shots.yml
# 4. 播种其余状态（零 LLM；有会话快照时聊天步自动跳过，否则这里会真问 4 个问题）：
uv run python scripts/seed_guide_instance.py --base http://127.0.0.1:8000 \
    --project-root <实例目录> --home <实例目录>/home
```

## 零 LLM 的边界（如实）

| 环节 | 是否零 LLM |
|---|---|
| KB / 用户 / 任务 / 订阅 / 技能 / 模板 / 判定 / 提案（快照 + 脚本） | ✅ |
| 32 张走播种会话的截图 | ✅ |
| 重新问 4 个播种问题 | 仅当不携带会话快照 |
| 分析面板、归因卡两张截图 + README GIF | ❌ 架构上需要真跑（面板步骤是运行时流出的，恢复的会话不带步骤；见 `capture-guide-shots.mjs` 内注释） |

刷新快照：在活的实例上重跑 `--snapshot-out`（播种/重截之后）。
