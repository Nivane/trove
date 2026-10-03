<!--
  InboxTable — 收件箱八类待办的条目级列表(§2.3)。

  纯呈现(与 DataTable 同一哲学:渲染行、发出意图):
    · 行内 diff 展开 —— 展开态是**呈现状态**,不进 URL(§3.2 的键表已冻结);
    · 每行的动作按钮按 `actionable` 显示:confirm / reject / 编辑后批准(深链);
      漂移条目在本页只有「去 Tab3 裁定」—— 裁定要看 live schema 对比(§2.3);
      行动两类(模板/提案)只有深链:审批闸与状态机在行动页,本页不就地处置;
    · 空态分两种:首次空(八类都是 0,把闸门讲明白)与筛选后空(清除筛选)。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DataTableColumn, DataTableSort } from '../base/DataTable.vue'
import DataTable from '../base/DataTable.vue'
import DiffCard from './DiffCard.vue'
import { t } from '../../i18n'
import { useReadOnly } from '../../composables/useReadOnly'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import type { GovernanceTodoItem } from '../../api/types'

const { readOnly, canOpen } = useReadOnly()

const props = withDefaults(
  defineProps<{
    items: GovernanceTodoItem[]
    loading?: boolean
    selected?: readonly (string | number)[]
    sort?: DataTableSort | null
    /** 正在执行动作的行 id(按钮转圈、防重复提交)。 */
    busyId?: string | null
    /** 有筛选(清除筛选入口随之出现)。 */
    filtered?: boolean
    /** 服务端总数;null = 未取到(显示 —,不是 0)。 */
    total?: number | null
    /**
     * total 是下界(切片里有降级腿,漏取的条目没算进来)→ 显示「≥ N」,
     * 不显示假精确值(设计稿 §6-B 单独验收)。判定在页面侧(degraded[] 与服务端
     * 切片语义只有那里知道),这里只负责渲染。
     */
    totalFloor?: boolean
  }>(),
  {
    loading: false, selected: () => [], sort: null, busyId: null,
    filtered: false, total: null, totalFloor: false,
  },
)

const emit = defineEmits<{
  (e: 'update:selected', value: (string | number)[]): void
  (e: 'update:sort', value: DataTableSort | null): void
  (e: 'confirm', item: GovernanceTodoItem): void
  (e: 'reject', item: GovernanceTodoItem): void
  (e: 'go-drift', item: GovernanceTodoItem): void
  (e: 'open-edit', item: GovernanceTodoItem): void
  (e: 'clear-filters'): void
}>()

const ui = useUiStore()

/** 行内 diff 展开(每次只展开一行 —— 表格是窄的,diff 是宽的)。 */
const expandedId = ref<string>('')

function toggleDiff(item: GovernanceTodoItem) {
  expandedId.value = expandedId.value === item.id ? '' : item.id
}

const u = (row: unknown) => row as GovernanceTodoItem

const columns = computed<DataTableColumn[]>(() => [
  { key: 'kind', label: t('govInboxType', ui.lang) },
  { key: 'title', label: t('govInboxItem', ui.lang) },
  { key: 'ds', label: t('govInboxSource', ui.lang) },
  { key: 'conf', label: t('govInboxConfSev', ui.lang) },
  {
    key: 'created_at',
    label: t('govInboxCreated', ui.lang),
    sortable: true,
    defaultDir: 'desc',
  },
  { key: 'actions', label: t('govInboxActions', ui.lang) },
])

/** kind → 展示名(八类 + 兜底原样透出)。 */
const KIND_KEY: Record<string, Parameters<typeof t>[0]> = {
  kb_lesson: 'govKindKbLesson',
  kb_example: 'govKindKbExample',
  semantic_draft: 'govKindSemanticDraft',
  skill_draft: 'govKindSkillDraft',
  memory_preference: 'govKindMemoryPref',
  drift: 'govKindDrift',
  action_template: 'govKindActionTemplate',
  action_proposal: 'govKindActionProposal',
}

/** 行动两类:深链文案要说清去处 —— 点进去是审批闸,不是"编辑后批准"。 */
const ACTION_KINDS = new Set(['action_template', 'action_proposal'])

function editLabel(kind: string): string {
  return ACTION_KINDS.has(kind)
    ? t('govInboxGoActions', ui.lang)
    : t('govInboxEditApprove', ui.lang)
}

function kindLabel(kind: string): string {
  const key = KIND_KEY[kind]
  return key ? t(key, ui.lang) : kind
}

function confText(item: GovernanceTodoItem): string {
  if (typeof item.confidence === 'number') return item.confidence.toFixed(2)
  if (item.severity) return item.severity
  return '—'
}

/** 干跑失败(diff.error)折成 DiffCard 的 validation —— 「算不出来」要可见。 */
function diffValidation(item: GovernanceTodoItem) {
  const err = item.diff?.error
  if (!err) return null
  return { ok: false, errors: [err], warnings: [] }
}

</script>

<template>
  <div class="inbox-table">
    <DataTable
      :columns="columns"
      :rows="items"
      row-key="id"
      :selectable="!readOnly"
      :selected="props.selected"
      :sort="props.sort"
      :loading="loading"
      :skeleton-rows="6"
      :select-all-label="t('govSelectAll', ui.lang)"
      :select-row-label="t('govSelectRow', ui.lang)"
      @update:selected="emit('update:selected', $event)"
      @update:sort="emit('update:sort', $event)"
    >
      <template #cell-kind="{ row }">
        <span class="kind-pill" :class="`is-${u(row).kind}`">{{ kindLabel(u(row).kind) }}</span>
      </template>

      <template #cell-title="{ row }">
        <div class="it-cell">
          <span class="it-title">{{ u(row).title }}</span>
          <span v-if="u(row).summary" class="it-summary">{{ u(row).summary }}</span>
          <div class="it-diff-bar">
            <button
              v-if="u(row).diff"
              type="button"
              class="link-btn"
              @click.stop="toggleDiff(u(row))"
            >
              {{ expandedId === u(row).id ? t('govInboxHideDiff', ui.lang) : t('govInboxShowDiff', ui.lang) }}
            </button>
            <span v-else class="it-no-diff">{{ t('govInboxDiffMissing', ui.lang) }}</span>
            <span v-if="u(row).source" class="it-source">{{ u(row).source }}</span>
          </div>
          <DiffCard
            v-if="u(row).diff && expandedId === u(row).id"
            :kind="u(row).kind"
            :name="u(row).title"
            :action="u(row).diff?.action ?? ''"
            :before="u(row).diff?.before ?? null"
            :after="u(row).diff?.after ?? null"
            :validation="diffValidation(u(row))"
          />
        </div>
      </template>

      <template #cell-ds="{ row }">
        <span v-if="u(row).ds" class="ds-chip">{{ u(row).ds }}</span>
        <span v-else class="it-global">{{ t('govInboxGlobal', ui.lang) }}</span>
      </template>

      <template #cell-conf="{ row }">
        <span
          v-if="u(row).severity"
          class="sev-pill"
          :class="`is-${u(row).severity}`"
          :title="u(row).severity ?? undefined"
        >{{ confText(u(row)) }}</span>
        <span v-else-if="typeof u(row).confidence === 'number'" class="conf-val" :title="String(u(row).confidence)">
          {{ confText(u(row)) }}
        </span>
        <span v-else class="it-none">—</span>
      </template>

      <template #cell-created_at="{ row }">
        <span class="it-time">{{ fmtDateTime(u(row).created_at ?? undefined) || '—' }}</span>
      </template>

      <template #cell-actions="{ row }">
        <div class="row-actions">
          <!-- 漂移条目:本页不裁定,深链 Tab3(§2.3)。 -->
          <a
            v-if="u(row).kind === 'drift'"
            class="link-btn"
            :href="`/admin/governance?tab=drift&ds=${encodeURIComponent(u(row).ds || '')}`"
            @click.prevent="emit('go-drift', u(row))"
          >
            {{ t('govInboxGoTab3', ui.lang) }}
          </a>
          <template v-else>
            <button
              v-if="!readOnly && u(row).actionable.confirm"
              type="button"
              class="act-btn is-primary"
              :disabled="busyId === u(row).id"
              @click.stop="emit('confirm', u(row))"
            >
              {{ t('govInboxConfirm', ui.lang) }}
            </button>
            <button
              v-if="!readOnly && u(row).actionable.reject"
              type="button"
              class="act-btn"
              :disabled="busyId === u(row).id"
              @click.stop="emit('reject', u(row))"
            >
              {{ t('govInboxReject', ui.lang) }}
            </button>
            <a
              v-if="u(row).actionable.edit_url && canOpen(u(row).actionable.edit_url ?? '')"
              class="link-btn"
              :href="u(row).actionable.edit_url ?? '#'"
              @click.prevent="emit('open-edit', u(row))"
            >
              {{ editLabel(u(row).kind) }}
            </a>
          </template>
        </div>
      </template>

      <template #empty>
        <div class="inbox-empty">
          <template v-if="filtered">
            <p class="ie-title">{{ t('govInboxNoMatch', ui.lang) }}</p>
            <button type="button" class="link-btn" @click="emit('clear-filters')">
              {{ t('semClearFilters', ui.lang) }}
            </button>
          </template>
          <template v-else>
            <p class="ie-title">{{ t('govInboxEmpty', ui.lang) }}</p>
            <p class="ie-desc">{{ t('govInboxEmptyDesc', ui.lang) }}</p>
          </template>
        </div>
      </template>
    </DataTable>

    <p class="it-total">
      {{
        total === null
          ? t('govNotFetched', ui.lang)
          : totalFloor
            ? t('govInboxTotalAtLeast', ui.lang, total)
            : t('govInboxTotal', ui.lang, total)
      }}
    </p>
  </div>
</template>

<style scoped>
.inbox-table {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.kind-pill {
  padding: 0 6px;
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  border: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.it-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 220px;
}
.it-title {
  font-weight: 600;
}
.it-summary {
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.it-diff-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-2xs);
}
.it-source,
.it-global,
.it-none,
.it-no-diff {
  color: var(--text-tertiary);
}
.ds-chip {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
.sev-pill {
  padding: 0 6px;
  border-radius: var(--r-sm);
  font-size: var(--fs-2xs);
  border: 1px solid var(--border-subtle);
}
.sev-pill.is-critical {
  color: var(--danger, #d64545);
  border-color: var(--danger, #d64545);
}
.sev-pill.is-warning {
  color: var(--text-primary);
}
.sev-pill.is-info {
  color: var(--text-tertiary);
}
.conf-val {
  font-variant-numeric: tabular-nums;
}
.it-time {
  color: var(--text-tertiary);
  white-space: nowrap;
}
.row-actions {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.act-btn {
  padding: 1px 8px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.act-btn.is-primary {
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
.act-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.link-btn {
  border: none;
  background: none;
  padding: 0;
  color: var(--accent);
  font-size: var(--fs-2xs);
  cursor: pointer;
  text-decoration: underline;
}
.inbox-empty {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  padding: var(--sp-3);
  text-align: center;
}
.ie-title {
  margin: 0;
  font-weight: 600;
}
.ie-desc {
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.it-total {
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
