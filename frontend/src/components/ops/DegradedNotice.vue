<!--
  DegradedNotice — degraded[] 是一等返回,不是错误侧信道(设计稿 §4.6)。
  某条腿超时/抛错时:HTTP 仍 200,该块为 null,这里列出 block/source/error
  与发生时刻;error 只有异常类型名(驱动原文不回传 —— /v1/health 的纪律)。
  整页级失败(存储探不通)不走这里:那种情况由页面顶部单独呈现。
-->
<script setup lang="ts">
import { TriangleAlert } from 'lucide-vue-next'
import type { OpsDegradedEntry } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'

withDefaults(
  defineProps<{
    items: OpsDegradedEntry[]
    /** 整页级失败时收起「其余部分照常」那句(它不成立)。 */
    pageLevel?: boolean
  }>(),
  { pageLevel: false },
)

const ui = useUiStore()
</script>

<template>
  <div v-if="items.length" class="degraded-notice" role="status">
    <div class="dn-head">
      <TriangleAlert :size="15" aria-hidden="true" />
      <span class="dn-title">{{ t('opsDegradedTitle', ui.lang) }}</span>
      <span v-if="!pageLevel" class="dn-desc">{{ t('opsDegradedDesc', ui.lang) }}</span>
    </div>
    <ul class="dn-list">
      <li v-for="(d, i) in items" :key="`${d.block}-${d.source}-${i}`" class="dn-item">
        <code class="dn-block">{{ d.block }}</code>
        <code class="dn-source" :title="d.source">{{ d.source }}</code>
        <span class="dn-error">{{ d.error }}</span>
        <span class="dn-at">{{ fmtDateTime(d.at) }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.degraded-notice {
  border: 1px solid var(--warn-border, var(--border-default));
  border-left: 3px solid var(--warn, #d97706);
  border-radius: var(--r-md);
  background: var(--warn-soft, var(--surface-sunken));
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.dn-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.dn-title {
  font-weight: 600;
  color: var(--text-primary);
}
.dn-list {
  margin: var(--sp-1) 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.dn-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.dn-block,
.dn-source {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dn-source {
  max-width: 320px;
}
.dn-error {
  font-weight: 600;
  color: var(--danger, #b91c1c);
}
.dn-at {
  margin-left: auto;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
