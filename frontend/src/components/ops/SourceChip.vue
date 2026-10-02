<!--
  SourceChip — 说明一个数字是「从哪读来的」(设计稿 §3.4 的 opsSrc* 键)。
  运营台的所有数字都有出处:文件、审计表、Prometheus 快照、KB 的 YAML。
  出处不是装饰:同一个问题在 serve 模式有审计、在 CLI/嵌入式没有 ——
  把两种情形渲染成同一个样子就是撒谎。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Activity, FileText, Library, ScrollText } from 'lucide-vue-next'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    /** file | audit | metrics | kb | process */
    kind: string
    /** 覆盖默认文案(如具体文件名/计数器名)。 */
    label?: string
  }>(),
  { label: '' },
)

const ui = useUiStore()

const ICONS: Record<string, unknown> = {
  file: FileText,
  audit: ScrollText,
  metrics: Activity,
  kb: Library,
  process: Activity,
}

const KEY: Record<string, string> = {
  file: 'opsSrcFile',
  audit: 'opsSrcAudit',
  metrics: 'opsSrcMetrics',
  kb: 'opsSrcKb',
  process: 'opsLifetimeProcess',
}

const icon = computed(() => ICONS[props.kind] ?? FileText)
const text = computed(() =>
  props.label
    ? props.label
    : t((KEY[props.kind] ?? 'opsSrcFile') as Parameters<typeof t>[0], ui.lang),
)
</script>

<template>
  <span class="source-chip" :title="text">
    <component :is="icon" :size="12" aria-hidden="true" />
    <span class="sc-text">{{ text }}</span>
  </span>
</template>

<style scoped>
.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 1px 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
}
.sc-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
