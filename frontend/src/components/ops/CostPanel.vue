<!--
  CostPanel — 成本块(设计稿 §4.2 cost)。口径 = assistant 消息的
  token_usage 元数据投影(裁决 ③/④:没有新表,读侧算)。

  两个必须显式说出来的事实:
    · ``sample_capped`` 时求和字段回 null —— 截断过的和是错的。页面显示
      「采样到顶」而不是把部分和当总数;
    · ``unmeasured`` = 没有 token_usage 元数据的 assistant 消息。这个分母
      是诚实的边界:说不清多少条没计量,剩下的数字就没有意义。
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { OpsCost } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import KpiTile from '../base/KpiTile.vue'
import SourceChip from './SourceChip.vue'

const props = defineProps<{ cost: OpsCost | null }>()

const ui = useUiStore()

function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

const percent = computed(() => {
  const ratio = props.cost?.unmeasured?.ratio
  if (ratio === null || ratio === undefined) return '—'
  return `${(ratio * 100).toFixed(1)}%`
})
</script>

<template>
  <section class="ops-card">
    <header class="card-head">
      <h3 class="card-title">{{ t('opsCostTitle', ui.lang) }}</h3>
      <SourceChip kind="audit" :label="t('opsCostSource', ui.lang)" />
    </header>

    <template v-if="cost">
      <div class="ops-kpi-row">
        <KpiTile
          :label="t('opsCostTotal', ui.lang)"
          :value="num(cost.tokens?.total)"
          :sub="cost.sampled === null ? '' : t('opsCostSampled', ui.lang, cost.sampled)"
        />
        <KpiTile
          :label="t('opsCostPrompt', ui.lang)"
          :value="num(cost.tokens?.prompt)"
        />
        <KpiTile
          :label="t('opsCostCompletion', ui.lang)"
          :value="num(cost.tokens?.completion)"
        />
        <KpiTile
          :label="t('opsCostCacheTokens', ui.lang)"
          :value="num(cost.cache_tokens)"
        />
        <KpiTile
          :label="t('opsCostUnmeasured', ui.lang)"
          :value="num(cost.unmeasured.without_usage)"
          :sub="t('opsCostUnmeasuredRatio', ui.lang, percent)"
        />
      </div>

      <p v-if="cost.sample_capped" class="ops-note is-warn">
        {{ t('opsCostCapped', ui.lang, cost.sample_max) }}
      </p>
      <p v-else-if="cost.sampled === null" class="ops-note">
        {{ t('opsCostUnwired', ui.lang) }}
      </p>
      <p v-else-if="cost.tokens === null" class="ops-note">
        {{ t('opsCostNoMeasurement', ui.lang) }}
      </p>

      <div class="cost-meta">
        <span v-if="cost.per_question" class="cost-meta-item">
          {{ t('opsCostPerQuestion', ui.lang) }}:
          {{ t('opsCostMean', ui.lang) }} {{ num(cost.per_question.mean_total) }} ·
          {{ t('opsCostMedian', ui.lang) }} {{ num(cost.per_question.median_total) }}
        </span>
        <span class="cost-meta-item">{{ t('opsCostUnmeasuredDesc', ui.lang) }}</span>
      </div>
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
.cost-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: var(--sp-2);
}
.cost-meta-item {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
