<!--
  FeedbackStrip — KB 反馈(裁决 ①:票数在 quality 端点,不另开端点)。

  口径上的诚实:
    · ``last_rated_at`` 是「有票词条的最后修改时间」,不是「最后一次点赞」
      (KB 不存分事件时间)—— 标签就按这个写;
    · ``promotion_enabled`` 是配置事实,不是运行结论;净赞门槛与置信门槛
      都来自后端,页面不猜。
-->
<script setup lang="ts">
import type { OpsFeedback } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import KpiTile from '../base/KpiTile.vue'
import SourceChip from './SourceChip.vue'

defineProps<{ feedback: OpsFeedback | null }>()

const ui = useUiStore()

function promotionText(fb: OpsFeedback): string {
  return fb.promotion_enabled
    ? t('opsFeedbackPromotionOn', ui.lang, fb.promotion_net_upvotes_min)
    : t('opsFeedbackPromotionOff', ui.lang)
}
</script>

<template>
  <section class="ops-card">
    <header class="card-head">
      <h3 class="card-title">{{ t('opsFeedbackTitle', ui.lang) }}</h3>
      <SourceChip kind="kb" />
    </header>

    <template v-if="feedback">
      <div class="ops-kpi-row">
        <KpiTile :label="t('opsFeedbackUp', ui.lang)" :value="feedback.up" />
        <KpiTile :label="t('opsFeedbackDown', ui.lang)" :value="feedback.down" />
        <KpiTile
          :label="t('opsFeedbackPendingLessons', ui.lang)"
          :value="feedback.pending_lessons"
        />
        <KpiTile
          :label="t('opsFeedbackConfirmedLessons', ui.lang)"
          :value="feedback.confirmed_lessons"
        />
        <KpiTile
          :label="t('opsFeedbackPendingExamples', ui.lang)"
          :value="feedback.pending_examples"
        />
        <KpiTile
          :label="t('opsFeedbackPromotion', ui.lang)"
          :value="feedback.promotion_enabled ? 'on' : 'off'"
          :sub="promotionText(feedback)"
        />
      </div>

      <div class="fb-meta">
        <span v-if="feedback.last_rated_at" class="fb-meta-item">
          {{ t('opsFeedbackLastRated', ui.lang) }}: {{ fmtDateTime(feedback.last_rated_at) }}
        </span>
        <span v-if="feedback.by_datasource.length" class="fb-meta-item">
          {{ t('opsFeedbackByDs', ui.lang) }}:
          <span v-for="(row, i) in feedback.by_datasource" :key="row.datasource">
            <template v-if="i > 0"> · </template>
            {{ row.datasource }} ↑{{ row.up }} ↓{{ row.down }}
          </span>
        </span>
        <span v-else class="fb-meta-item">{{ t('opsFeedbackNoVotes', ui.lang) }}</span>
      </div>
    </template>

    <p v-else class="ops-note">{{ t('opsFeedbackUnavailable', ui.lang) }}</p>
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
.fb-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  margin-top: var(--sp-2);
}
.fb-meta-item {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ops-note {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
