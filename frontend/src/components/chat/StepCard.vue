<template>
  <details class="step-card" :class="[`node-${cssNode}`, `status-${status}`]" :open="status !== 'done'">
    <summary>
      <span class="step-dot" />
      <span class="step-node">{{ displayLabel }}</span>
      <span v-if="attempt > 1" class="step-attempt">· {{ t('attempt', ui.lang, attempt) }}</span>
      <span v-if="elapsedMs != null" class="step-elapsed">{{ elapsedMs }}</span>
      <span v-else-if="status !== 'done' && liveMs != null" class="step-elapsed live">{{
        fmtMs(liveMs)
      }}</span>
    </summary>
    <div class="step-body">
      <SqlBlock v-if="view.sql" :code="view.sql" />
      <template v-else-if="node === 'execute_sql'">
        <div class="kv-line">
          <span class="k">{{ t('rows', ui.lang) }}</span><span class="v">{{ view.rowCount ?? '–' }}</span>
          <span v-if="view.timeMs != null" class="k">{{ t('timeLabel', ui.lang) }}</span><span v-if="view.timeMs != null" class="v">{{
            fmtMs(view.timeMs)
          }}</span>
        </div>
        <MarkdownView v-if="view.text" :source="view.text" />
      </template>
      <MarkdownView v-else-if="view.text" :source="view.text" />
      <div v-else-if="status !== 'done'" class="step-empty">
        <LoaderCircle :size="13" class="spin" />
        <span>{{ t('runningNow', ui.lang) }}</span>
      </div>
      <div v-else class="step-empty">—</div>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { LoaderCircle } from 'lucide-vue-next'
import MarkdownView from './MarkdownView.vue'
import SqlBlock from './SqlBlock.vue'
import { useUiStore } from '../../stores/ui'
import { extractStep, stepLabel, fmtMs } from '../../utils/steps'
import type { StepCard as StepCardType } from '../../stores/chat'
import { t } from '../../i18n'

const props = defineProps<{
  card: StepCardType
  status?: 'done' | 'running' | 'error'
  attempt?: number
  /** Live elapsed (ms) shown on a running/error step. */
  liveMs?: number | null
}>()
const ui = useUiStore()

const status = computed(() => props.status ?? 'done')
const node = computed(() => props.card.node)
const cssNode = computed(() => node.value.replace(/[^a-z0-9_]/gi, '_'))
const displayLabel = computed(
  () => props.card.label || stepLabel(node.value, ui.lang),
)
const payload = computed(() => props.card.payload)
const view = computed(() => extractStep(props.card.payload))
const attempt = computed(() => props.attempt ?? 0)
const elapsedMs = computed(() => {
  if (!payload.value) return null
  const ms = (payload.value as { elapsed_ms?: number }).elapsed_ms
  return typeof ms === 'number' && ms >= 0 ? fmtMs(ms) : null
})
const liveMs = computed(() => {
  const ms = props.liveMs
  return typeof ms === 'number' && ms >= 0 ? ms : null
})
</script>
