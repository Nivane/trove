<template>
  <details class="step-card" :class="[`node-${cssNode}`, `status-${status}`]" :open="status !== 'done'">
    <summary>
      <span class="step-dot" />
      <span class="step-node">{{ displayLabel }}</span>
      <span v-if="view.fastPath" class="chip chip-fast">{{ t('fastPathChip', ui.lang) }}</span>
      <span v-if="view.kbExact" class="chip chip-exact">{{ t('kbExactChip', ui.lang) }}</span>
      <span v-if="view.complexity" class="chip chip-complexity">{{ view.complexity }}</span>
      <span v-if="view.forced" class="chip chip-forced">{{ t('forcedChip', ui.lang) }}</span>
      <span v-if="view.backend" class="chip chip-backend" :title="t('retrievalBackend', ui.lang)">{{ backendLabel(view.backend, ui.lang) }}</span>
      <span v-if="view.memoryBackend" class="chip chip-memory" :title="t('memoryBackend', ui.lang)">{{ t(memoryLabelKey(view.memoryBackend), ui.lang) }}</span>
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

      <!-- route_intent: 证据链 -->
      <div v-else-if="view.intentEvidence" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('intentSignals', ui.lang) }}</span>
          <span v-if="view.intentEvidence.signals.length" class="chips">
            <span v-for="s in view.intentEvidence.signals" :key="s" class="chip chip-signal">{{ s }}</span>
          </span>
          <span v-else class="v">—</span>
        </div>
        <div v-if="view.intentEvidence.llmVerdict" class="link-row">
          <span class="k">{{ t('intentLlm', ui.lang) }}</span>
          <span class="v">{{ view.intentEvidence.llmVerdict }}</span>
        </div>
        <div v-if="view.intentEvidence.llmError" class="link-row">
          <span class="k">{{ t('intentLlmError', ui.lang) }}</span>
          <span class="mono">{{ view.intentEvidence.llmError }}</span>
        </div>
        <div v-if="view.intentEvidence.termHit || view.intentEvidence.mentionedTable || view.intentEvidence.rewritten || view.intentEvidence.substituted" class="link-row">
          <span class="k">{{ t('intentFlags', ui.lang) }}</span>
          <span class="chips">
            <span v-if="view.intentEvidence.termHit" class="chip chip-term">term</span>
            <span v-if="view.intentEvidence.mentionedTable" class="chip">table</span>
            <span v-if="view.intentEvidence.rewritten" class="chip">{{ t('rewritten', ui.lang) }}</span>
            <span v-if="view.intentEvidence.substituted" class="chip">{{ t('substituted', ui.lang) }}</span>
          </span>
        </div>
      </div>

      <!-- query_sketch: 编译决策 + 计划校验 -->
      <div v-else-if="view.compile || view.planValidation" class="link-detail">
        <div v-if="view.compile" class="link-row">
          <span class="k">{{ t('compileOutcome', ui.lang) }}</span>
          <span class="chips">
            <span class="chip" :class="view.compile.outcome === 'compiled' ? 'chip-exact' : 'chip-warn'">
              {{ view.compile.outcome ?? '—' }}
            </span>
            <span v-if="view.compile.missComponent" class="chip">{{ view.compile.missComponent }}</span>
          </span>
        </div>
        <div v-if="view.compile?.missReason" class="link-row">
          <span class="k">{{ t('compileMissReason', ui.lang) }}</span>
          <span class="mono">{{ view.compile.missReason }}</span>
        </div>
        <div v-if="view.planValidation" class="link-row">
          <span class="k">{{ t('planCheck', ui.lang) }}</span>
          <span class="v">{{ view.planValidation.status ?? '—' }}</span>
        </div>
        <div v-if="view.planValidation?.errors?.length" class="link-row">
          <span class="k">{{ t('planCheckErrors', ui.lang) }}</span>
          <span class="mono">{{ view.planValidation.errors.join('; ') }}</span>
        </div>
      </div>

      <!-- select: 投票归因 + 置信度 -->
      <div v-else-if="view.selection" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('consensus', ui.lang) }}</span>
          <span class="chips">
            <span class="chip" :class="view.selection.adopted !== false ? 'chip-exact' : 'chip-warn'">
              {{ view.selection.adopted !== false ? t('adopted', ui.lang) : t('notAdopted', ui.lang) }}
            </span>
            <span v-if="view.selection.winner" class="chip">{{ view.selection.winner }}</span>
            <span v-if="view.selection.degraded" class="chip chip-warn">{{ view.selection.degraded }}</span>
            <span v-if="typeof view.selection.confidence === 'number'" class="chip">
              {{ t('confidence', ui.lang) }} {{ (view.selection.confidence * 100).toFixed(0) }}%
            </span>
          </span>
        </div>
        <div v-if="view.selection.votes && Object.keys(view.selection.votes).length" class="link-row">
          <span class="k">{{ t('votes', ui.lang) }}</span>
          <span class="mono">{{ Object.entries(view.selection.votes).map(([k, n]) => `${k}=${n}`).join(', ') }}</span>
        </div>
      </div>

      <!-- validate: 规则链 -->
      <div v-else-if="view.validationHits?.length" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('ruleHits', ui.lang) }}</span>
          <span class="chips">
            <span v-for="(h, i) in view.validationHits" :key="i" class="chip chip-warn" :title="h.reason">
              {{ h.rule }}
            </span>
          </span>
        </div>
      </div>

      <!-- analyze_error: 修复模式 / 回归进展 / 版本链 -->
      <div v-else-if="view.fixMode || view.sqlVersions?.length" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('fixMode', ui.lang) }}</span>
          <span class="chips">
            <span v-if="view.fixMode" class="chip">{{ view.fixMode }}</span>
            <span v-if="view.lastProgress" class="chip" :class="view.lastProgress === 'improved' ? 'chip-exact' : 'chip-warn'">{{ view.lastProgress }}</span>
            <span v-if="view.noProgressRounds" class="chip chip-warn">{{ t('noProgress', ui.lang, view.noProgressRounds) }}</span>
          </span>
        </div>
        <div v-if="view.sqlVersions?.length" class="link-row">
          <span class="k">{{ t('sqlVersions', ui.lang) }}</span>
          <span class="mono">
            {{ view.sqlVersions.map((v) => `#${v.round}[${(v.issues || []).join(',') || (v.error || '?')}]`).join(' → ') }}
          </span>
        </div>
      </div>

      <!-- chart: 图表判定(是否画图 / 图型 / 维度 / 度量) -->
      <div v-else-if="node === 'chart'" class="link-detail">
        <div class="link-row">
          <span class="k">{{ t('chartDecision', ui.lang) }}</span>
          <span class="chips">
            <span class="chip" :class="view.chartDecision?.chartable ? 'chip-exact' : 'chip-warn'">
              {{ view.chartDecision?.chartable ? t('chartChartable', ui.lang) : t('noChart', ui.lang) }}
            </span>
            <span v-if="view.chartDecision?.type" class="chip chip-signal">{{ view.chartDecision.type }}</span>
            <span v-if="view.chartDecision?.source" class="chip" :class="view.chartDecision.source === 'llm' ? 'chip-llm' : 'chip-deterministic'">
              {{ view.chartDecision.source === 'llm' ? t('chartSourceLlm', ui.lang) : t('chartSourceDeterministic', ui.lang) }}
            </span>
          </span>
        </div>
        <div v-if="view.chartDecision?.dimension" class="link-row">
          <span class="k">{{ t('chartDimension', ui.lang) }}</span>
          <span class="mono">{{ view.chartDecision.dimension }}</span>
        </div>
        <div v-if="view.chartDecision?.measures?.length" class="link-row">
          <span class="k">{{ t('chartMeasures', ui.lang) }}</span>
          <span class="chips">
            <span v-for="m in view.chartDecision.measures" :key="m" class="chip chip-term">{{ m }}</span>
          </span>
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
      <div v-else-if="view.contextUsage?.length" class="kv-line">
        <span class="k">{{ t('contextUsage', ui.lang) }}</span>
        <span class="mono">{{ view.contextUsage.map((c) => `${c.block}:${c.tokens}`).join(', ') }}</span>
      </div>
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
import { extractStep, stepLabel, backendLabel, memoryLabelKey, fmtMs } from '../../utils/steps'
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
