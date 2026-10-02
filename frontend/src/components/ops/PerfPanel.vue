<!--
  PerfPanel — 延迟 + 缓存 + 预算/降级(设计稿 §4.2 latency / cache / budget)。

  三块的口径差别很大,页面照实分开:
    · 延迟来自 audit_log(query.execute 行的 details.execution_time_ms)。
      只有 serve 模式有这张表的行 —— 窗口内没有记录是"没测到",不是 0ms;
    · 缓存连接器命中是进程内计数器(重启清零),prompt 命中率来自基线
      scorecard(离线文件);答案缓存本轮不测;
    · 预算/降级/熔断是 Prometheus 快照,**进程生命周期**而不是窗口 ——
      快照计数器没有时间维度,套窗口就是编。
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { OpsBudget, OpsBudgetRow, OpsCache, OpsLatency } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import KpiTile from '../base/KpiTile.vue'
import SourceChip from './SourceChip.vue'

const props = defineProps<{
  latency: OpsLatency | null
  cache: OpsCache | null
  budget: OpsBudget | null
}>()

const ui = useUiStore()

function ms(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${v.toLocaleString()} ms`
}

function budgetRows(rows: OpsBudgetRow[] | undefined): OpsBudgetRow[] {
  return rows ?? []
}

const cacheRate = computed(() => {
  const r = props.cache?.prompt?.cache_hit_rate
  if (r === null || r === undefined) return '—'
  return `${(r * 100).toFixed(1)}%`
})
</script>

<template>
  <section class="ops-card">
    <header class="card-head">
      <h3 class="card-title">{{ t('opsPerfTitle', ui.lang) }}</h3>
      <SourceChip kind="audit" :label="latency?.basis || t('opsSrcAudit', ui.lang)" />
    </header>

    <template v-if="latency">
      <div class="ops-kpi-row">
        <KpiTile :label="t('opsPerfP50', ui.lang)" :value="ms(latency.p50_ms)" :sub="t('opsPerfN', ui.lang, latency.n)" />
        <KpiTile :label="t('opsPerfP95', ui.lang)" :value="ms(latency.p95_ms)" />
        <KpiTile
          :label="t('opsPerfEndToEnd', ui.lang)"
          :value="'—'"
          :sub="t('opsPerfEndToEndNull', ui.lang)"
        />
      </div>
      <p v-if="latency.sample_capped" class="ops-note is-warn">
        {{ t('opsPerfSampleCapped', ui.lang) }}
      </p>
      <p v-if="latency.n === 0" class="ops-note">{{ t('opsPerfNoData', ui.lang) }}</p>

      <table v-if="latency.series.length" class="ops-table">
        <caption class="ops-caption">{{ t('opsPerfSeries', ui.lang) }}</caption>
        <thead>
          <tr>
            <th scope="col">{{ t('opsPerfDate', ui.lang) }}</th>
            <th scope="col" class="is-num">{{ t('opsPerfP50', ui.lang) }}</th>
            <th scope="col" class="is-num">{{ t('opsBudgetCount', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in latency.series" :key="row.date">
            <td>{{ row.date }}</td>
            <td class="is-num">{{ ms(row.p50_ms) }}</td>
            <td class="is-num">{{ row.n }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <p v-else class="ops-note">{{ t('opsBlockUnavailable', ui.lang) }}</p>
  </section>

  <section class="ops-card">
    <header class="card-head">
      <h3 class="card-title">{{ t('opsCacheTitle', ui.lang) }}</h3>
      <SourceChip kind="metrics" />
    </header>
    <div class="ops-kpi-row">
      <KpiTile
        :label="t('opsCacheConnector', ui.lang)"
        :value="cache?.connector ? cache.connector.hits.toLocaleString() : '—'"
        :sub="t('opsLifetimeProcess', ui.lang)"
      />
      <KpiTile
        :label="t('opsCachePrompt', ui.lang)"
        :value="cacheRate"
        :sub="cache?.prompt ? cache.prompt.source : t('opsCacheNoScorecard', ui.lang)"
      />
      <KpiTile
        :label="t('opsCacheAnswer', ui.lang)"
        :value="'—'"
        :sub="t('opsCacheNotWired', ui.lang)"
      />
    </div>
  </section>

  <section class="ops-card">
    <header class="card-head">
      <h3 class="card-title">{{ t('opsBudgetTitle', ui.lang) }}</h3>
      <SourceChip kind="metrics" :label="t('opsLifetimeProcess', ui.lang)" />
    </header>
    <p class="ops-note">{{ t('opsBudgetNote', ui.lang) }}</p>

    <template v-if="budget">
      <table v-if="budgetRows(budget.decisions).length" class="ops-table">
        <caption class="ops-caption">{{ t('opsBudgetDecisions', ui.lang) }}</caption>
        <thead>
          <tr>
            <th scope="col">{{ t('opsBudgetDatasource', ui.lang) }}</th>
            <th scope="col">{{ t('opsBudgetSourceCol', ui.lang) }}</th>
            <th scope="col">{{ t('opsBudgetVerdict', ui.lang) }}</th>
            <th scope="col" class="is-num">{{ t('opsBudgetCount', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in budget.decisions" :key="`d-${i}`">
            <td>{{ row.datasource || '—' }}</td>
            <td>{{ row.source || '—' }}</td>
            <td>{{ row.verdict || '—' }}</td>
            <td class="is-num">{{ row.count }}</td>
          </tr>
        </tbody>
      </table>

      <table v-if="budgetRows(budget.degraded).length" class="ops-table">
        <caption class="ops-caption">{{ t('opsBudgetDegraded', ui.lang) }}</caption>
        <thead>
          <tr>
            <th scope="col">{{ t('opsBudgetDatasource', ui.lang) }}</th>
            <th scope="col" class="is-num">{{ t('opsBudgetCount', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in budget.degraded" :key="`g-${i}`">
            <td>{{ row.datasource || '—' }}</td>
            <td class="is-num">{{ row.count }}</td>
          </tr>
        </tbody>
      </table>

      <table v-if="budgetRows(budget.kills).length" class="ops-table">
        <caption class="ops-caption">{{ t('opsBudgetKills', ui.lang) }}</caption>
        <thead>
          <tr>
            <th scope="col">{{ t('opsBudgetDatasource', ui.lang) }}</th>
            <th scope="col">{{ t('opsBudgetResult', ui.lang) }}</th>
            <th scope="col" class="is-num">{{ t('opsBudgetCount', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in budget.kills" :key="`k-${i}`">
            <td>{{ row.datasource || '—' }}</td>
            <td>{{ row.result || '—' }}</td>
            <td class="is-num">{{ row.count }}</td>
          </tr>
        </tbody>
      </table>

      <p
        v-if="
          !budget.decisions.length && !budget.degraded.length && !budget.kills.length
        "
        class="ops-note"
      >
        {{ t('opsBudgetEmpty', ui.lang) }}
      </p>
    </template>
    <p v-else class="ops-note">{{ t('opsBlockUnavailable', ui.lang) }}</p>
  </section>
</template>

<style scoped>
.ops-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.card-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.ops-kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: var(--sp-2);
}
.ops-note {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ops-note.is-warn {
  color: var(--warn, #b45309);
}
.ops-table {
  width: 100%;
  margin-top: var(--sp-2);
  border-collapse: collapse;
  font-size: var(--fs-2xs);
}
.ops-caption {
  text-align: left;
  padding: 2px 0;
  color: var(--text-secondary);
  font-weight: 600;
}
.ops-table th,
.ops-table td {
  padding: 3px 6px;
  border-bottom: 1px solid var(--border-subtle);
  text-align: left;
  color: var(--text-secondary);
}
.ops-table .is-num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
</style>
