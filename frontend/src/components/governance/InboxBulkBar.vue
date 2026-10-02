<!--
  InboxBulkBar — 选择条 + 批量结果(§2.3)。

  两条口径:
    · **批量只对同类条目开放** —— 按钮按类分组渲染(本示例:语义草稿 ×1、
      KB 教训 ×1,故两个按钮各 1 条)。语义草稿走批量端点,其余类走逐条
      端点,两者归一到同一个结果形状 {id, ok, error};
    · **部分失败要明细,不是一句 toast** —— 成功/失败逐条列出,失败带原因
      并留在列表里可单条重试(§6-2)。「空列表 ≠ 成功」,所以全成功时
      有一句显式文案。

  纯呈现:动作由页面执行,结果由页面回传。
-->
<script lang="ts">
export interface BulkGroup {
  kind: string
  ds: string
  /** 该组的展示名(调用方已翻译:如「语义草稿」)。 */
  label: string
  ids: string[]
}

export interface BulkResultRow {
  id: string
  title: string
  ok: boolean
  error: string
}
</script>

<script setup lang="ts">
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

withDefaults(
  defineProps<{
    selectedCount: number
    groups: BulkGroup[]
    busy?: boolean
    results?: BulkResultRow[]
    /** 结果是否已回来(空结果也要显式说「全部成功」)。 */
    hasResults?: boolean
  }>(),
  { busy: false, results: () => [], hasResults: false },
)

const emit = defineEmits<{
  (e: 'confirm', group: BulkGroup): void
  (e: 'reject', group: BulkGroup): void
  (e: 'clear'): void
  (e: 'retry', row: BulkResultRow): void
}>()

const ui = useUiStore()
</script>

<template>
  <div v-if="selectedCount > 0 || hasResults" class="bulk-bar">
    <div v-if="selectedCount > 0" class="bb-row">
      <span class="bb-count">{{ t('govBulkSelected', ui.lang, selectedCount) }}</span>
      <span class="bb-hint">{{ t('govBulkHint', ui.lang) }}</span>
      <span class="bb-spacer" />
      <template v-for="group in groups" :key="`${group.kind}:${group.ds}`">
        <button
          type="button"
          class="bb-btn is-primary"
          :disabled="busy"
          @click="emit('confirm', group)"
        >
          {{ t('govBulkConfirmKind', ui.lang, group.ids.length) }} · {{ group.label }}
        </button>
        <button
          type="button"
          class="bb-btn"
          :disabled="busy"
          @click="emit('reject', group)"
        >
          {{ t('govBulkRejectKind', ui.lang, group.ids.length) }} · {{ group.label }}
        </button>
      </template>
      <button type="button" class="bb-btn" :disabled="busy" @click="emit('clear')">
        {{ t('govBulkClear', ui.lang) }}
      </button>
    </div>

    <p v-if="busy" class="bb-running">{{ t('govBulkRunning', ui.lang) }}</p>

    <div v-if="hasResults" class="bb-results">
      <div class="bb-results-title">{{ t('govBulkResult', ui.lang) }}</div>
      <p v-if="results.length === 0" class="bb-all-ok">{{ t('govBulkAllOk', ui.lang) }}</p>
      <table v-else class="bb-table">
        <thead>
          <tr>
            <th>{{ t('govInboxItem', ui.lang) }}</th>
            <th>{{ t('govBulkResult', ui.lang) }}</th>
            <th>{{ t('govBulkReason', ui.lang) }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in results" :key="row.id" :class="{ 'is-bad': !row.ok }">
            <td class="bb-item">{{ row.title || row.id }}</td>
            <td>
              <span class="bb-status" :class="row.ok ? 'is-ok' : 'is-bad'">
                {{ row.ok ? t('govBulkOk', ui.lang) : t('govBulkFailed', ui.lang) }}
              </span>
            </td>
            <td class="bb-err">{{ row.error || '—' }}</td>
            <td>
              <button
                v-if="!row.ok"
                type="button"
                class="bb-btn"
                :disabled="busy"
                @click="emit('retry', row)"
              >
                {{ t('retry', ui.lang) }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.bulk-bar {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: var(--r-md);
  background: var(--accent-soft, var(--surface-raised));
  margin-bottom: var(--sp-2);
}
.bb-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.bb-count {
  font-weight: 600;
}
.bb-hint,
.bb-running {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.bb-running {
  margin: 0;
}
.bb-spacer {
  flex: 1;
}
.bb-btn {
  padding: 1px 8px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.bb-btn.is-primary {
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
.bb-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.bb-results-title {
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-tertiary);
}
.bb-all-ok {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
}
.bb-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-2xs);
  margin-top: 2px;
}
.bb-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.bb-table td {
  padding: 3px 10px 3px 0;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
}
.bb-status.is-ok {
  color: var(--text-primary);
}
.bb-status.is-bad {
  color: var(--danger, #d64545);
  font-weight: 600;
}
.bb-err {
  color: var(--text-secondary);
  word-break: break-word;
}
.bb-item {
  font-weight: 600;
}
</style>
