<template>
  <details class="step-card" :class="`node-${cssNode}`">
    <summary>
      <span class="step-dot"></span>
      <span class="step-node">{{ label }}</span>
    </summary>
    <div class="step-body">
      <template v-if="node === 'gen_sql'">
        <pre class="code-block sql"><code>{{ payload.sql }}</code></pre>
      </template>
      <template v-else-if="node === 'execute_sql'">
        <div class="kv-line">
          <span class="k">rows</span><span class="v">{{ payload.row_count ?? '–' }}</span>
          <span class="k">time</span><span class="v">{{ fmtDuration(payload.execution_time_ms) }}</span>
        </div>
      </template>
      <template v-else>
        <MarkdownView v-if="text" :source="text" />
      </template>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownView from './MarkdownView.vue'
import { fmtDuration } from '../../utils/format'
import type { StepCard as StepCardType } from '../../stores/chat'

const props = defineProps<{ card: StepCardType }>()

const node = computed(() => props.card.node)
const cssNode = computed(() => node.value.replace(/[^a-z0-9_]/gi, '_'))
const label = computed(() => props.card.label || node.value)

const text = computed(() => {
  const c = props.card.payload.content
  if (typeof c === 'string') return c
  return ''
})
</script>
