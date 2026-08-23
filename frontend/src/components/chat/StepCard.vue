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
      <!-- schema_linking: 匹配与来源(分析面板核心) -->
      <div v-if="view.link" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('matchedTables', ui.lang) }}</span>
          <span v-if="view.link.tables.length" class="chips">
            <span v-for="tb in view.link.tables" :key="tb" class="chip">
              {{ tb }}
              <span v-if="view.link.notesTables.includes(tb)" class="chip-src" :title="t('srcSchemaNotes', ui.lang)">
                notes
              </span>
            </span>
          </span>
          <span v-else class="v">—</span>
        </div>
        <div v-if="view.link.terms.length" class="link-row">
          <span class="k">{{ t('srcSemantics', ui.lang) }}</span>
          <span class="chips">
            <span v-for="tq in view.link.terms" :key="tq" class="chip chip-term">{{ tq }}</span>
          </span>
        </div>
        <div v-if="view.link.valueHits.length" class="link-row">
          <span class="k">{{ t('srcValues', ui.lang) }}</span>
          <span class="mono">{{ view.link.valueHits.join(' · ') }}</span>
        </div>
        <div v-if="view.link.fieldHits.length" class="link-row">
          <span class="k">{{ t('srcFields', ui.lang) }}</span>
          <span class="mono">{{ view.link.fieldHits.join(' · ') }}</span>
        </div>
        <div v-if="view.link.relations" class="link-row">
          <span class="k">{{ t('srcRelations', ui.lang) }}</span>
          <span class="v">{{ t('yes', ui.lang) }}</span>
        </div>
        <div v-if="view.text && view.text !== (view.link.tables.join(', '))" class="link-log">
          <details>
            <summary>{{ t('ctxLog', ui.lang) }}</summary>
            <pre class="ctx-pre">{{ view.text }}</pre>
          </details>
        </div>
      </div>
      <SqlBlock v-else-if="view.sql" :code="view.sql" />
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
