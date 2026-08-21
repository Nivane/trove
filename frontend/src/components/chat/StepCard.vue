<template>
  <details class="step-card" :class="`node-${cssNode}`">
    <summary>
      <span class="step-dot"></span>
      <span class="step-node">{{ displayLabel }}</span>
      <span v-if="elapsedMs != null" class="step-elapsed">{{ elapsedMs }}</span>
    </summary>
    <div class="step-body">
      <SqlBlock v-if="view.sql" :code="view.sql" />
      <template v-else-if="node === 'execute_sql'">
        <div class="kv-line">
          <span class="k">rows</span><span class="v">{{ view.rowCount ?? '–' }}</span>
          <span v-if="view.timeMs != null" class="k">time</span><span v-if="view.timeMs != null" class="v">{{ fmtMs(view.timeMs) }}</span>
        </div>
        <MarkdownView v-if="view.text" :source="view.text" />
      </template>
      <MarkdownView v-else-if="view.text" :source="view.text" />
      <div v-else class="step-empty">—</div>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownView from './MarkdownView.vue'
import SqlBlock from './SqlBlock.vue'
import { useUiStore } from '../../stores/ui'
import { extractStep, stepLabel, fmtMs } from '../../utils/steps'
import type { StepCard as StepCardType } from '../../stores/chat'

const props = defineProps<{ card: StepCardType }>()
const ui = useUiStore()

const node = computed(() => props.card.node)
const cssNode = computed(() => node.value.replace(/[^a-z0-9_]/gi, '_'))
const displayLabel = computed(() =>
  props.card.label || stepLabel(node.value, ui.lang),
)
const payload = computed(() => props.card.payload)
const view = computed(() => extractStep(props.card.payload))
const elapsedMs = computed(() => {
  if (!payload.value) return null
  const ms = (payload.value as { elapsed_ms?: number }).elapsed_ms
  return typeof ms === 'number' && ms >= 0 ? fmtMs(ms) : null
})
</script>