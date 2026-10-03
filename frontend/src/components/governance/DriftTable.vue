<!--
  DriftTable — Tab3 的漂移列表 + 三条不能含糊的状态(§2.5,§6-A 最重要的一条)。

  ① 503 / skipped ≠ 空:「检测未能完成 · skip_reason」横幅 + 上一次结果标「已过期」,
     列表**绝不清空为 0 条**;
  ② 从未检测 ≠ 没有漂移:首跑态给「立即检测」主 CTA;
  ③ null 与 0 分开:计数不是数字时渲染「—」。

  纯呈现:裁定/检测由 GovernanceView 执行。
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { DataTableColumn } from '../base/DataTable.vue'
import DataTable from '../base/DataTable.vue'
import StatePanel from '../base/StatePanel.vue'
import { t } from '../../i18n'
import { useReadOnly } from '../../composables/useReadOnly'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import type { GovernanceDriftItem } from '../../api/types'

const { readOnly, canOpen } = useReadOnly()

withDefaults(
  defineProps<{
    ds: string
    items: GovernanceDriftItem[]
    loading?: boolean
    /** 该源跑过检测(有 runs 记录)。false = 从未检测。 */
    checked?: boolean
    /** 最近一次未完成(503/跳过),items 是上一次成功的结果。 */
    stale?: boolean
    skipReason?: string
    /** 有筛选(清除筛选入口随之出现)。 */
    filtered?: boolean
  }>(),
  { loading: false, checked: true, stale: false, skipReason: '', filtered: false },
)

const emit = defineEmits<{
  (e: 'detail', item: GovernanceDriftItem): void
  (e: 'resolve', item: GovernanceDriftItem): void
  (e: 'waive', item: GovernanceDriftItem): void
  (e: 'recheck'): void
  (e: 'go-datasources'): void
  (e: 'clear-filters'): void
}>()

const ui = useUiStore()

const columns = computed<DataTableColumn[]>(() => [
  { key: 'level', label: t('govDriftColLevel', ui.lang) },
  { key: 'subject', label: t('govDriftColSubject', ui.lang) },
  { key: 'severity', label: t('govDriftColSeverity', ui.lang) },
  { key: 'life', label: t('govDriftColLife', ui.lang) },
  { key: 'status', label: t('govDriftColStatus', ui.lang) },
  { key: 'actions', label: t('govDriftColActions', ui.lang) },
])

const u = (row: unknown) => row as GovernanceDriftItem

const STATUS_KEY: Record<string, Parameters<typeof t>[0]> = {
  open: 'govDriftStatusOpen',
  waived: 'govDriftStatusWaived',
  resolved: 'govDriftStatusResolved',
}

function statusLabel(s: string): string {
  const key = STATUS_KEY[s]
  return key ? t(key, ui.lang) : s
}

function lifeText(item: GovernanceDriftItem): string {
  const parts: string[] = []
  if (item.first_seen_at) {
    parts.push(t('govDriftDetailSeen', ui.lang, fmtDateTime(item.first_seen_at) || item.first_seen_at))
  }
  if (typeof item.seen_count === 'number') {
    parts.push(t('govDriftDetailSeenCount', ui.lang, item.seen_count))
  }
  return parts.join(' · ') || '—'
}
</script>

<template>
  <div class="drift-table">
    <!-- ① 未完成:横幅常驻,条目保留但标「已过期」。 -->
    <div v-if="stale" class="dt-skip" role="alert">
      <div class="dt-skip-head">
        <span class="dt-skip-title">{{ t('govDriftSkipped', ui.lang) }} · {{ ds }}</span>
        <span v-if="skipReason" class="dt-skip-reason">
          {{ t('govDriftSkipReasonLabel', ui.lang) }}: <code>{{ skipReason }}</code>
        </span>
        <span class="dt-skip-levels">
          {{ t('govDriftLevelsVerifiedLabel', ui.lang) }}: <code>[]</code>
        </span>
      </div>
      <p class="dt-skip-explain">{{ t('govDriftSkipExplain', ui.lang) }}</p>
      <div class="dt-skip-actions">
        <button v-if="!readOnly" type="button" class="act-btn is-primary" :disabled="loading" @click="emit('recheck')">
          {{ t('govDriftCheck', ui.lang) }}
        </button>
        <button v-if="canOpen('/admin/datasources')" type="button" class="act-btn" @click="emit('go-datasources')">
          {{ t('govDriftGoDatasources', ui.lang) }}
        </button>
      </div>
    </div>

    <DataTable
      :columns="columns"
      :rows="items"
      row-key="id"
      :loading="loading"
      :skeleton-rows="4"
    >
      <template #cell-level="{ row }">
        <span class="dt-level">{{ u(row).level }}</span>
      </template>

      <template #cell-subject="{ row }">
        <div class="dt-cell">
          <span class="dt-subject">{{ u(row).subject }}</span>
          <span class="dt-meta">
            kind: {{ u(row).kind }} · source: {{ u(row).source }}
          </span>
        </div>
      </template>

      <template #cell-severity="{ row }">
        <span class="dt-sev" :class="`is-${u(row).severity}`">{{ u(row).severity }}</span>
      </template>

      <template #cell-life="{ row }">
        <div class="dt-cell">
          <span class="dt-meta">{{ lifeText(u(row)) }}</span>
          <span v-if="stale" class="dt-stale">{{ t('govDriftStale', ui.lang) }}</span>
        </div>
      </template>

      <template #cell-status="{ row }">
        <span class="dt-status" :class="`is-${u(row).status}`">{{ statusLabel(u(row).status) }}</span>
      </template>

      <template #cell-actions="{ row }">
        <div class="dt-actions">
          <button type="button" class="act-btn" @click="emit('detail', u(row))">
            {{ t('opsViewDetail', ui.lang) }}
          </button>
          <template v-if="u(row).status === 'open'">
            <button v-if="!readOnly" type="button" class="act-btn is-primary" @click="emit('resolve', u(row))">
              {{ t('govDriftResolve', ui.lang) }}
            </button>
            <button v-if="!readOnly" type="button" class="act-btn" @click="emit('waive', u(row))">
              {{ t('govDriftWaive', ui.lang) }}
            </button>
          </template>
        </div>
      </template>

      <template #empty>
        <div class="dt-empty">
          <!-- ② 从未检测:主 CTA 是「立即检测」,不是「没有漂移」。 -->
          <StatePanel
            v-if="!checked && !stale"
            mode="empty"
            :title="t('govDriftUnchecked', ui.lang)"
            :description="t('govDriftUncheckedDesc', ui.lang)"
            :retry-text="t('govDriftCheckNow', ui.lang)"
            @retry="emit('recheck')"
          />
          <!-- ③ 检测未完成 + 上次也空:说清「这次没检查成」,不写「0 条」。 -->
          <StatePanel
            v-else-if="stale"
            mode="empty"
            :title="t('govDriftStale', ui.lang)"
            :description="t('govDriftStaleNote', ui.lang)"
          />
          <template v-else>
            <p class="dt-empty-title">{{ t('govDriftEmpty', ui.lang) }}</p>
            <button v-if="filtered" type="button" class="link-btn" @click="emit('clear-filters')">
              {{ t('semClearFilters', ui.lang) }}
            </button>
          </template>
        </div>
      </template>
    </DataTable>
  </div>
</template>

<style scoped>
.drift-table {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.dt-skip {
  border: 1px solid var(--danger, #d64545);
  border-left: 3px solid var(--danger, #d64545);
  border-radius: var(--r-md);
  background: var(--danger-soft, var(--surface-sunken));
  padding: var(--sp-2) var(--sp-3);
}
.dt-skip-head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: baseline;
}
.dt-skip-title {
  font-weight: 700;
  color: var(--danger, #d64545);
}
.dt-skip-reason,
.dt-skip-levels {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.dt-skip-explain {
  margin: 4px 0 6px;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.dt-skip-actions {
  display: flex;
  gap: 8px;
}
.dt-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 200px;
}
.dt-level {
  font-family: var(--font-mono, monospace);
  font-weight: 600;
}
.dt-subject {
  font-weight: 600;
}
.dt-meta {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.dt-stale {
  font-size: var(--fs-2xs);
  color: var(--warn, #d97706);
}
.dt-sev.is-critical {
  color: var(--danger, #d64545);
  font-weight: 600;
}
.dt-sev.is-warning {
  color: var(--text-primary);
}
.dt-sev.is-info {
  color: var(--text-tertiary);
}
.dt-status {
  font-size: var(--fs-2xs);
  padding: 0 6px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-subtle);
  white-space: nowrap;
}
.dt-status.is-open {
  border-color: var(--danger, #d64545);
  color: var(--danger, #d64545);
}
.dt-actions {
  display: flex;
  gap: 6px;
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
.dt-empty {
  padding: var(--sp-3);
  text-align: center;
}
.dt-empty-title {
  margin: 0;
  font-weight: 600;
}
</style>
