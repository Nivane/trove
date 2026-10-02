<!--
  VersionTimeline — Tab3 的版本区(§2.5)。

  两条口径:
    · 非 git 环境返回**空列表**(不是错误)—— 空态文案覆盖这一种;
    · 回滚按钮挂在每个提交上,但爆炸半径的说明常驻(端点名字只写 semantic,
      恢复的却是该源整个 KB 目录)—— 具体清单在 RollbackDialog 里运行时取。

  纯呈现:取数与执行在 GovernanceView。
-->
<script setup lang="ts">
import StatePanel from '../base/StatePanel.vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import type { SemanticHistoryEntry } from '../../api/types'

withDefaults(
  defineProps<{
    ds: string
    entries: SemanticHistoryEntry[]
    loading?: boolean
    error?: string
  }>(),
  { loading: false, error: '' },
)

const emit = defineEmits<{
  (e: 'rollback', entry: SemanticHistoryEntry): void
  (e: 'retry'): void
}>()

const ui = useUiStore()

function shortSha(sha: string): string {
  return (sha || '').slice(0, 7) || '—'
}
</script>

<template>
  <section class="ver-timeline">
    <div class="vt-head">
      <h3 class="vt-title">{{ t('govVerTitle', ui.lang) }}</h3>
      <span class="vt-ds mono">{{ ds }}</span>
    </div>

    <StatePanel v-if="loading" mode="loading" :title="t('govLoading', ui.lang)" />
    <StatePanel
      v-else-if="error"
      mode="error"
      :title="t('govVerError', ui.lang)"
      :detail="error"
      :retry-text="t('retry', ui.lang)"
      @retry="emit('retry')"
    />
    <StatePanel
      v-else-if="entries.length === 0"
      mode="empty"
      :title="t('govVerEmpty', ui.lang)"
    />
    <table v-else class="vt-table">
      <thead>
        <tr>
          <th>{{ t('govVerColSha', ui.lang) }}</th>
          <th>{{ t('govVerColSubject', ui.lang) }}</th>
          <th>{{ t('govVerColTrailers', ui.lang) }}</th>
          <th>{{ t('govVerColDate', ui.lang) }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in entries" :key="e.sha">
          <td class="mono">{{ shortSha(e.sha) }}</td>
          <td class="vt-subject">{{ e.subject }}</td>
          <td class="vt-trailers mono">{{ e.trailers || '—' }}</td>
          <td class="mono vt-date">{{ fmtDateTime(e.date) || '—' }}</td>
          <td>
            <button type="button" class="act-btn" @click="emit('rollback', e)">
              {{ t('govVerRollback', ui.lang) }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <p class="vt-danger">
      <strong>{{ t('govVerDanger', ui.lang) }}</strong>
      {{ t('govVerDangerDesc', ui.lang) }}
    </p>
  </section>
</template>

<style scoped>
.ver-timeline {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
}
.vt-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: var(--sp-2);
}
.vt-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
}
.vt-ds {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.vt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.vt-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.vt-table td {
  padding: 4px 10px 4px 0;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
.vt-subject {
  font-weight: 600;
}
.vt-trailers {
  white-space: pre-line;
  color: var(--text-secondary);
}
.vt-date {
  white-space: nowrap;
}
.vt-danger {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
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
</style>
