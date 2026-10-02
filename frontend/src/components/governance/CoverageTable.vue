<!--
  CoverageTable — Tab2 的逐源覆盖表(§2.4)。

  三条诚实渲染(§6-B/C/D):
    · 物理侧拿不到(physical:null)→ 有几分之几的分子照给,分母渲染
      「未取到」+ 原因,绝不画成 0;
    · `uncovered_tables` / `asked_unmodeled` 为 null = 差不可得(有一侧的腿
      没取到),与「空集合」分开渲染;
    · 「被问了但没建模」清单是**行动入口**:每行「去建模」深链到语义页的
      对应数据源(不是首页,§6-5)。

  纯呈现:筛选与取数在 GovernanceView。
-->
<script lang="ts">
import type { GovernanceCoverageDegraded, GovernanceCoverageSource } from '../../api/types'

export interface CoverageGoModel {
  ds: string
  table: string
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { DataTableColumn } from '../base/DataTable.vue'
import DataTable from '../base/DataTable.vue'
import StatePanel from '../base/StatePanel.vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'

const props = withDefaults(
  defineProps<{
    sources: GovernanceCoverageSource[]
    degraded?: GovernanceCoverageDegraded[]
    loading?: boolean
    /** 有筛选(清除筛选入口随之出现)。 */
    filtered?: boolean
  }>(),
  { degraded: () => [], loading: false, filtered: false },
)

const emit = defineEmits<{
  (e: 'open-details', ds: string): void
  (e: 'go-model', payload: CoverageGoModel): void
  (e: 'clear-filters'): void
}>()

const ui = useUiStore()

const columns = computed<DataTableColumn[]>(() => [
  { key: 'ds', label: t('govCovDs', ui.lang) },
  { key: 'model', label: t('govCovModel', ui.lang) },
  { key: 'uncovered', label: t('govCovUncovered', ui.lang) },
  { key: 'asked', label: t('govCovAsked', ui.lang) },
  { key: 'refused', label: t('govCovRefused', ui.lang) },
  { key: 'actions', label: '' },
])

const u = (row: unknown) => row as GovernanceCoverageSource

/** 该源是否有一条腿降级(ds 级;原因挂在行上,不是全局横幅)。 */
function degradedReason(ds: string): string {
  const hit = props.degraded.find((d) => d.ds === ds)
  return hit?.error ?? ''
}

function modelTop(s: GovernanceCoverageSource): string {
  if (!s.model) return '—'
  const declared = s.model.declared_tables?.length ?? 0
  const physical = s.physical?.tables
  return `${declared} / ${physical ?? '—'}`
}

function modelSub(s: GovernanceCoverageSource): string {
  if (!s.model) return t('govNotFetched', ui.lang)
  if (s.model.enabled === false || (s.model.declared_tables?.length ?? 0) === 0) {
    return t('govCovModelOff', ui.lang)
  }
  return `${t('govCovDatasets', ui.lang, s.model.datasets)} · ${t('govCovMetrics', ui.lang, s.model.metrics)}`
}

/** 未建模的表:null = 差不可得;[] = 真的没有(—);否则计数 + 前几个名字。 */
function uncoveredText(s: GovernanceCoverageSource): string {
  const list = s.uncovered_tables
  if (list === null) return '—'
  if (list.length === 0) return '—'
  const all = s.physical?.tables
  if (all !== null && all !== undefined && list.length >= all) {
    return t('govCovTablesAll', ui.lang, list.length)
  }
  return t('govCovTablesN', ui.lang, list.length)
}

function uncoveredNames(s: GovernanceCoverageSource): string {
  const list = s.uncovered_tables ?? []
  const head = list.slice(0, 4).join(' · ')
  return list.length > 4 ? `${head} …` : head
}

function askedText(s: GovernanceCoverageSource): string {
  const list = s.asked_unmodeled
  if (list === null || list.length === 0) return '—'
  return t('govCovTablesN', ui.lang, list.length)
}

/** 「被问了但没建模」清单(跨源;每行带源名,深链去建模)。 */
const askedRows = computed(() =>
  props.sources.flatMap((s) =>
    (s.asked_unmodeled ?? []).map((a) => ({ ds: s.ds, ...a })),
  ),
)
</script>

<template>
  <div class="coverage-table">
    <DataTable
      :columns="columns"
      :rows="sources"
      row-key="ds"
      :loading="loading"
      :skeleton-rows="4"
    >
      <template #cell-ds="{ row }">
        <div class="ct-ds">
          <span class="ct-ds-name">{{ u(row).ds }}</span>
          <span
            v-if="degradedReason(u(row).ds)"
            class="ct-partial"
            :title="degradedReason(u(row).ds)"
          >{{ t('govPartial', ui.lang) }}</span>
        </div>
      </template>

      <template #cell-model="{ row }">
        <div class="ct-cell">
          <span class="ct-strong">{{ modelTop(u(row)) }}</span>
          <span
            class="ct-sub"
            :class="{ 'is-missing': !u(row).model || u(row).model?.enabled === false }"
          >{{ modelSub(u(row)) }}</span>
        </div>
      </template>

      <template #cell-uncovered="{ row }">
        <div class="ct-cell">
          <span>{{ uncoveredText(u(row)) }}</span>
          <span v-if="u(row).uncovered_tables === null" class="ct-sub is-missing">
            {{ t('govCovLegGap', ui.lang) }}
          </span>
          <span v-else-if="uncoveredNames(u(row))" class="ct-sub mono">
            {{ uncoveredNames(u(row)) }}
          </span>
        </div>
      </template>

      <template #cell-asked="{ row }">
        <div class="ct-cell">
          <span>{{ askedText(u(row)) }}</span>
          <span v-if="u(row).asked_unmodeled === null" class="ct-sub is-missing">
            {{ t('govCovLegGap', ui.lang) }}
          </span>
        </div>
      </template>

      <template #cell-refused="{ row }">
        <div class="ct-cell">
          <span v-if="u(row).refused === null">—</span>
          <span v-else-if="u(row).refused?.count === 0">0</span>
          <span v-else>{{ t('govCovRefusedN', ui.lang, u(row).refused?.count ?? 0) }}</span>
          <button
            v-if="(u(row).refused?.count ?? 0) > 0"
            type="button"
            class="link-btn"
            @click="emit('open-details', u(row).ds)"
          >
            {{ t('govCovDetails', ui.lang) }}
          </button>
        </div>
      </template>

      <template #cell-actions="{ row }">
        <button type="button" class="link-btn" @click="emit('open-details', u(row).ds)">
          {{ t('govCovDetails', ui.lang) }}
        </button>
      </template>

      <template #empty>
        <div class="ct-empty">
          <template v-if="filtered">
            <p class="ct-empty-title">{{ t('govCovNoMatch', ui.lang) }}</p>
            <button type="button" class="link-btn" @click="emit('clear-filters')">
              {{ t('semClearFilters', ui.lang) }}
            </button>
          </template>
          <StatePanel
            v-else
            mode="empty"
            :title="t('govCovEmpty', ui.lang)"
            :description="t('govCovEmptyDesc', ui.lang)"
          >
            <template #action>
              <RouterLink class="link-btn" to="/admin/datasources">
                {{ t('govCovGoDatasources', ui.lang) }}
              </RouterLink>
            </template>
          </StatePanel>
        </div>
      </template>
    </DataTable>

    <section v-if="askedRows.length" class="ct-asked">
      <h3 class="ct-asked-title">{{ t('govCovAsked', ui.lang) }}</h3>
      <p class="ct-asked-desc">{{ t('govCovLegGapNote', ui.lang) }}</p>
      <table class="ct-asked-table">
        <thead>
          <tr>
            <th>{{ t('govLinColTable', ui.lang) }}</th>
            <th>{{ t('govCovDs', ui.lang) }}</th>
            <th>{{ t('govCovAskColAsked', ui.lang) }}</th>
            <th>{{ t('govCovLastAsked', ui.lang) }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in askedRows" :key="`${a.ds}:${a.table}`">
            <td class="mono">{{ a.table }}</td>
            <td class="mono">{{ a.ds }}</td>
            <td>{{ t('govCovQueries', ui.lang, a.queries) }}</td>
            <td class="mono">{{ fmtDateTime(a.last_asked_at ?? undefined) || '—' }}</td>
            <td>
              <a
                class="link-btn"
                :href="`/admin/semantic?ds=${encodeURIComponent(a.ds)}`"
                @click.prevent="emit('go-model', { ds: a.ds, table: a.table })"
              >
                {{ t('govCovGoModel', ui.lang) }}
              </a>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.coverage-table {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.ct-ds {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ct-ds-name {
  font-family: var(--font-mono, monospace);
  font-weight: 600;
}
.ct-partial {
  font-size: var(--fs-2xs);
  color: var(--warn, #d97706);
}
.ct-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.ct-strong {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.ct-sub {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ct-sub.is-missing,
.is-missing {
  color: var(--warn, #d97706);
}
.mono {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
}
.link-btn {
  border: none;
  background: none;
  padding: 0;
  color: var(--accent);
  font-size: var(--fs-2xs);
  cursor: pointer;
  text-decoration: underline;
  text-align: left;
}
.ct-empty {
  padding: var(--sp-3);
  text-align: center;
}
.ct-empty-title {
  margin: 0 0 4px;
  font-weight: 600;
}
.ct-asked {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  background: var(--surface-raised);
}
.ct-asked-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
}
.ct-asked-desc {
  margin: 2px 0 var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ct-asked-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.ct-asked-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.ct-asked-table td {
  padding: 3px 10px 3px 0;
  border-bottom: 1px solid var(--border-subtle);
}
</style>
