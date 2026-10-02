<!--
  DiffCard — 审批差的唯一渲染(线 0 冻结;治理中心 / 知识库 / 语义页三页共用)。

  props 清单由方案 §5.1③ 冻结,不得增删(§5.2:谁先做谁冻结,后做的不改):
      {kind, name, action, before, after, validation?, impact?}

  两条诚实点:
    · `before === null` 是**新建条目**(不是「没有变化」)—— 文案分开;
    · `impact` 是一处变更的爆炸半径快照;空快照照实说「暂无影响面快照」,
      不画空表(§6-D)。
  差的行由 before/after 现算(doc 未给 fields 之外的形状),字段并集 + 值比较,
  排序稳定,不依赖服务端的行序。
-->
<script lang="ts">
/** 干跑校验结果(与语义页 validate 端点同形状的最小子集)。 */
export interface DiffValidation {
  ok: boolean
  errors: string[]
  warnings: string[]
}

/** 影响面快照(与漂移详情同一形状:四个具名组,空组也是事实)。 */
export interface DiffImpact {
  metrics?: string[]
  examples?: string[]
  rules?: string[]
  lessons?: string[]
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    kind: string
    name: string
    action: string
    /**
     * 记录(实体原始视图)或整段文本(null = 该侧不存在)。
     * 两侧都是记录 → 按字段并集出行;任一侧是标量(如 KB 待审教训的
     * 提议文本)→ 单行呈现;两侧都空 → 「差不可得」。
     */
    before: unknown
    after: unknown
    validation?: DiffValidation | null
    impact?: DiffImpact | null
  }>(),
  { validation: null, impact: null },
)

const ui = useUiStore()

interface DiffRow {
  f: string
  before: string
  after: string
  changed: boolean
}

/** null/undefined 渲染成 '—'(未取到 ≠ 空字符串),对象折叠成 JSON。 */
function fmt(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/** 纯记录(排除数组与标量)—— 只有记录之间才谈得上字段并集。 */
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const rows = computed<DiffRow[]>(() => {
  const b = props.before
  const a = props.after
  if (isRecord(b) || isRecord(a)) {
    const bb = isRecord(b) ? b : {}
    const aa = isRecord(a) ? a : {}
    const keys = new Set<string>([...Object.keys(bb), ...Object.keys(aa)])
    return [...keys].sort().map((f) => {
      const before = fmt(bb[f])
      const after = fmt(aa[f])
      return { f, before, after, changed: before !== after }
    })
  }
  if (b === null || b === undefined) {
    if (a === null || a === undefined) return []
    // 新建条目:before 不存在,after 是提议内容(字符串)。
    return [
      { f: t('govDiffProposal', ui.lang), before: '—', after: fmt(a), changed: true },
    ]
  }
  return [{ f: t('govDiffProposal', ui.lang), before: fmt(b), after: fmt(a), changed: true }]
})

const isNew = computed(
  () => (props.before === null || props.before === undefined) && props.after != null,
)

const impactGroups = computed(() => {
  const impact = props.impact
  if (!impact) return []
  return (
    [
      ['metrics', impact.metrics ?? []],
      ['examples', impact.examples ?? []],
      ['rules', impact.rules ?? []],
      ['lessons', impact.lessons ?? []],
    ] as [string, string[]][]
  ).filter(([, items]) => items.length > 0)
})

const impactEmpty = computed(
  () => props.impact !== null && impactGroups.value.length === 0,
)
</script>

<template>
  <div class="diff-card">
    <div class="dc-head">
      <span class="dc-kind">{{ kind }}</span>
      <span class="dc-name">{{ name }}</span>
      <span v-if="action" class="dc-action">{{ action }}</span>
      <span v-if="isNew" class="dc-new">{{ t('govDiffNew', ui.lang) }}</span>
    </div>

    <table v-if="rows.length" class="dc-table">
      <thead>
        <tr>
          <th>{{ t('govDiffField', ui.lang) }}</th>
          <th>{{ t('govDiffBefore', ui.lang) }}</th>
          <th>{{ t('govDiffAfter', ui.lang) }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.f" :class="{ 'is-changed': row.changed }">
          <td class="dc-f">{{ row.f }}</td>
          <td class="dc-b">{{ row.before }}</td>
          <td class="dc-a">{{ row.after }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="dc-none">{{ t('govInboxDiffMissing', ui.lang) }}</p>

    <div v-if="validation" class="dc-validation" :class="{ 'is-bad': !validation.ok }">
      <p v-for="(e, i) in validation.errors" :key="`e${i}`" class="dc-err">{{ e }}</p>
      <p v-for="(w, i) in validation.warnings" :key="`w${i}`" class="dc-warn">{{ w }}</p>
    </div>

    <div v-if="impact" class="dc-impact">
      <div class="dc-impact-title">{{ t('govDriftImpact', ui.lang) }}</div>
      <p v-if="impactEmpty" class="dc-none">{{ t('govDriftImpactEmpty', ui.lang) }}</p>
      <div v-for="[group, items] in impactGroups" :key="group" class="dc-impact-group">
        <span class="dc-impact-label">{{ group }}</span>
        <span v-for="item in items" :key="item" class="dc-impact-item">{{ item }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.diff-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-2xs);
}
.dc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.dc-kind,
.dc-action {
  padding: 0 6px;
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  font-family: var(--font-mono, monospace);
}
.dc-name {
  font-weight: 600;
}
.dc-new {
  color: var(--accent);
  font-weight: 600;
}
.dc-table {
  width: 100%;
  border-collapse: collapse;
}
.dc-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 1px 8px 1px 0;
}
.dc-table td {
  padding: 1px 8px 1px 0;
  vertical-align: top;
  word-break: break-word;
}
.dc-f {
  color: var(--text-tertiary);
  white-space: nowrap;
}
.dc-b {
  color: var(--text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--border-default);
}
.dc-a {
  color: var(--text-primary);
}
.dc-none {
  margin: 2px 0;
  color: var(--text-tertiary);
}
.dc-validation {
  margin-top: 4px;
}
.dc-err {
  margin: 1px 0;
  color: var(--danger, #d64545);
}
.dc-warn {
  margin: 1px 0;
  color: var(--text-secondary);
}
.dc-impact {
  margin-top: 6px;
  border-top: 1px dashed var(--border-subtle);
  padding-top: 4px;
}
.dc-impact-title {
  color: var(--text-tertiary);
  font-weight: 600;
}
.dc-impact-group {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  margin-top: 2px;
}
.dc-impact-label {
  color: var(--text-tertiary);
}
.dc-impact-item {
  padding: 0 5px;
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
}
</style>
