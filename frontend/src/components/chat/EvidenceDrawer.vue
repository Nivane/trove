<!--
  依据抽屉(P1 答案可信层):SQL / 执行与计划要点 / 结果集预览 / 校验与反思 /
  反馈入口 / 运行轨迹 —— 一处收拢。数据全部来自已有落盘:
  终态 summary(会话库) + 只读回放端点 ``GET /v1/runs/{run_id}``。

  三道纪律:
  1. 拿不到就不显示 —— 每节、每行都按缺席整行不渲染,不编「未知」;
  2. 回放只读:不重跑 SQL、不重调模型(重跑必须走原问答入口的 RLS/脱敏/审计);
  3. 反馈回执不承诺「已采纳」:好评只累加统计,差评只落待确认草案。
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { t } from '../../i18n'
import DetailDrawer from '../base/DetailDrawer.vue'
import { apiGet } from '../../api/http'
import { copyText, fmtDuration, fmtVal } from '../../utils/format'
import { buildProvenance, sourceLabelKey } from '../../utils/provenance'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import type { RunReplay } from '../../api/types'
import type { Turn } from '../../stores/chat'

const props = defineProps<{
  modelValue: boolean
  turn: Turn | null
  /** 该轮在 store.turns 里的下标(反馈通道要它)。 */
  turnIndex: number
  /** 打开后滚到的锚点:'replay' = 运行轨迹。 */
  focus?: 'evidence' | 'replay'
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const chat = useChatStore()
const ui = useUiStore()
const lang = computed(() => ui.lang)

const summary = computed(() => props.turn?.summary ?? null)
const view = computed(() => buildProvenance(summary.value, props.turn?.at))

const copiedSql = ref(false)
const receipt = ref('')
const receiptFailed = ref(false)
const replay = ref<RunReplay | null>(null)
const replayLoading = ref(false)
const replayFailed = ref(false)
const replayEl = ref<HTMLElement | null>(null)

watch(
  () => [props.modelValue, summary.value?.run_id],
  async () => {
    copiedSql.value = false
    receipt.value = ''
    receiptFailed.value = false
    replay.value = null
    replayFailed.value = false
    if (!props.modelValue) return
    const runId = summary.value?.run_id
    if (!runId) return
    replayLoading.value = true
    try {
      replay.value = await apiGet<RunReplay>(
        `/v1/runs/${encodeURIComponent(runId)}`,
      )
    } catch {
      // 拿不到轨迹就不显示这一节(历史轮常有的情形)—— 不编一份过程。
      replayFailed.value = true
    } finally {
      replayLoading.value = false
    }
    if (props.focus === 'replay') {
      await nextTick()
      replayEl.value?.scrollIntoView({ block: 'start' })
    }
  },
  // 挂载时已开着也要取(测试/深链直接开抽屉的情形);关着时 immediate 一进来就早退,不请求。
  { immediate: true },
)

const headerChips = computed(() => {
  const x = view.value
  const chips: string[] = []
  if (x.datasource) chips.push(x.datasource)
  if (x.runShort) chips.push(`run ${x.runShort}`)
  if (x.time) chips.push(x.time)
  if (x.source) {
    const key = sourceLabelKey(x.source)
    chips.push(key ? t(key, lang.value) : x.source)
  }
  return chips
})

async function copySql() {
  const sql = summary.value?.sql
  if (!sql) return
  const ok = await copyText(sql)
  if (!ok) return
  copiedSql.value = true
  window.setTimeout(() => {
    copiedSql.value = false
  }, 1600)
}

/** 执行与计划要点:只报摘要里真有数据的行(方言/计划要点等拿不到就不出现)。 */
const execRows = computed(() => {
  const s = summary.value
  if (!s) return [] as { key: string; label: string; value: string }[]
  const ev = s.execution_evidence ?? {}
  const rows: { key: string; label: string; value: string }[] = []
  if (typeof s.row_count === 'number' && s.row_count >= 0) {
    rows.push({
      key: 'rows',
      label: t('provRowRows', lang.value),
      value: String(s.row_count),
    })
  }
  const execMs = execMsFromSteps.value
  if (execMs != null) {
    rows.push({
      key: 'ms',
      label: t('provRowExecMs', lang.value),
      value: fmtDuration(execMs),
    })
  }
  if (typeof ev.estimated_rows === 'number') {
    rows.push({
      key: 'scan',
      label: t('provRowScan', lang.value),
      value: ev.source
        ? `${fmtVal(ev.estimated_rows)}（${ev.source}）`
        : fmtVal(ev.estimated_rows),
    })
  }
  if (typeof ev.scanned_rows === 'number') {
    rows.push({
      key: 'scanned',
      label: t('provRowScanned', lang.value),
      value: fmtVal(ev.scanned_rows),
    })
  }
  if (typeof ev.limit_applied === 'number') {
    rows.push({
      key: 'limit',
      label: t('provRowLimit', lang.value),
      value: String(ev.limit_applied),
    })
  }
  if (ev.data_as_of) {
    rows.push({
      key: 'asof',
      label: t('provRowAsOf', lang.value),
      value:
        ev.as_of_basis && ev.as_of_basis !== 'unknown'
          ? `${ev.data_as_of}（${t('provAsOfBasis', lang.value)}：${ev.as_of_basis}）`
          : ev.data_as_of,
    })
  }
  if (ev.degraded || (ev.verdict && ev.verdict !== 'ok')) {
    rows.push({
      key: 'degraded',
      label: t('provRowDegraded', lang.value),
      value: [ev.verdict, ev.reason].filter(Boolean).join(' · ') || '—',
    })
  }
  return rows
})

/** 执行耗时(live 轮的 step payload 带着;历史轮拿不到就不显示)。 */
const execMsFromSteps = computed<number | null>(() => {
  const steps = props.turn?.steps ?? []
  for (let i = steps.length - 1; i >= 0; i--) {
    const p = steps[i].payload as { execution_time_ms?: unknown }
    if (typeof p?.execution_time_ms === 'number') return p.execution_time_ms
  }
  return null
})

const previewRows = computed(() => (summary.value?.rows ?? []).slice(0, 3))
const previewCols = computed(() => summary.value?.columns ?? [])

const checkRows = computed(() => {
  const s = summary.value
  if (!s) return [] as { key: string; main: string; sub: string }[]
  const rows: { key: string; main: string; sub: string }[] = []
  if (s.verdict) {
    rows.push({
      key: 'verdict',
      main: `${t('provCheckVerdict', lang.value)} = ${s.verdict}`,
      sub: 'reflect',
    })
  }
  for (const [i, e] of (s.confidence_evidence ?? []).entries()) {
    if (!e?.why) continue
    rows.push({
      key: `ev-${i}`,
      main: String(e.why),
      sub: [e.kind, e.key].filter(Boolean).join(' · '),
    })
  }
  return rows
})

function replayStepText(step: { elapsed_ms?: number | null; tokens?: { total?: number } | null; status?: string }): string {
  const parts: string[] = []
  if (step.elapsed_ms != null) parts.push(fmtDuration(step.elapsed_ms))
  if (typeof step.tokens?.total === 'number') parts.push(`${step.tokens.total} tok`)
  if (step.status === 'running') parts.push(t('provReplayStatusRunning', lang.value))
  return parts.join(' · ')
}

const replayNote = computed(() => {
  const r = replay.value
  if (!r) return ''
  if (r.source === 'session') return t('provReplaySessionOnly', lang.value)
  return r.complete === false
    ? t('provReplayIncomplete', lang.value)
    : t('provReplayComplete', lang.value)
})

async function submitVote(vote: 1 | -1) {
  if (props.turnIndex < 0) return
  receipt.value = ''
  receiptFailed.value = false
  const ok = await chat.rateTurn(props.turnIndex, vote)
  if (ok) {
    receipt.value = t(
      vote === 1 ? 'provFbReceiptUp' : 'provFbReceiptDown',
      lang.value,
    )
  } else {
    receiptFailed.value = true
  }
}
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    width="520px"
    :aria-label="t('provEvidenceTitle', lang)"
    :close-label="t('close', lang)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="ev-head">
        <h2 class="ev-title">{{ t('provEvidenceTitle', lang) }}</h2>
        <div class="ev-chips">
          <span v-for="c in headerChips" :key="c" class="ev-chip">{{ c }}</span>
        </div>
      </div>
    </template>

    <!-- 生成的 SQL -->
    <section v-if="summary?.sql" class="ev-sec">
      <h4 class="ev-h">{{ t('provSecSql', lang) }}</h4>
      <div class="ev-sqlbox">
        <div class="ev-sqlbar mono">
          <span>SQL</span>
          <button type="button" class="ev-mini" @click="copySql()">
            {{ copiedSql ? t('copied', lang) : t('copySql', lang) }}
          </button>
        </div>
        <pre class="ev-sql mono">{{ summary.sql }}</pre>
      </div>
    </section>

    <!-- 执行与计划要点 -->
    <section v-if="execRows.length" class="ev-sec">
      <h4 class="ev-h">{{ t('provSecExec', lang) }}</h4>
      <dl class="ev-kv">
        <div v-for="row in execRows" :key="row.key" class="ev-kvrow">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </div>
      </dl>
    </section>

    <!-- 结果集预览 -->
    <section v-if="previewRows.length" class="ev-sec">
      <h4 class="ev-h">
        {{ t('provSecResult', lang) }}
        <span class="ev-tag">
          {{
            typeof summary?.row_count === 'number' && summary.row_count > previewRows.length
              ? `${t('provPreviewFirst', lang, previewRows.length)} · ${t('provTotalRows', lang, summary.row_count)}`
              : t('provPreviewFirst', lang, previewRows.length)
          }}
        </span>
      </h4>
      <div class="ev-tablewrap">
        <table class="ev-table">
          <thead v-if="previewCols.length">
            <tr>
              <th v-for="(c, ci) in previewCols" :key="ci">{{ c }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in previewRows" :key="ri">
              <td v-for="(cell, ci) in row" :key="ci">{{ fmtVal(cell) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="ev-note">{{ t('provFullResultNote', lang) }}</p>
    </section>

    <!-- 校验与反思记录 -->
    <section v-if="checkRows.length" class="ev-sec">
      <h4 class="ev-h">{{ t('provSecChecks', lang) }}</h4>
      <div v-for="row in checkRows" :key="row.key" class="ev-check">
        <span class="ev-check-main">{{ row.main }}</span>
        <span v-if="row.sub" class="ev-check-sub mono">{{ row.sub }}</span>
      </div>
    </section>

    <!-- 反馈入口(复用现有评分通道 POST /v1/kb/ratings) -->
    <section class="ev-sec">
      <h4 class="ev-h">{{ t('provSecFeedback', lang) }}</h4>
      <div class="ev-fbrow">
        <button
          type="button"
          class="ev-fbbtn"
          :class="{ active: turn?.rating === 1 }"
          @click="submitVote(1)"
        >
          👍 {{ t('provFbUp', lang) }}
        </button>
        <button
          type="button"
          class="ev-fbbtn"
          :class="{ active: turn?.rating === -1 }"
          @click="submitVote(-1)"
        >
          👎 {{ t('provFbDown', lang) }}
        </button>
      </div>
      <p v-if="receipt" class="ev-receipt">{{ receipt }}</p>
      <p v-if="receiptFailed" class="ev-receipt warn">{{ t('provFbFail', lang) }}</p>
      <p class="ev-note">{{ t('provFbNote', lang) }}</p>
    </section>

    <!-- 运行轨迹(只读回放) -->
    <section
      v-if="summary?.run_id && !replayFailed"
      ref="replayEl"
      class="ev-sec"
    >
      <h4 class="ev-h">
        {{ t('provSecReplay', lang) }}
        <span v-if="replayNote" class="ev-tag">{{ replayNote }}</span>
      </h4>
      <p v-if="replayLoading" class="ev-note">{{ t('provReplayLoading', lang) }}</p>
      <template v-else-if="replay">
        <div v-if="replay.timeline?.length" class="ev-replay">
          <div class="ev-replay-cap">{{ t('provReplayNode', lang) }}</div>
          <div v-for="(s, i) in replay.timeline" :key="`${s.name}-${i}`" class="ev-step">
            <span class="ev-step-name mono">{{ s.name }}</span>
            <span class="ev-step-meta">{{ replayStepText(s) }}</span>
          </div>
        </div>
        <div v-if="replay.llm_calls?.length" class="ev-replay">
          <div class="ev-replay-cap">{{ t('provReplayLlm', lang) }}</div>
          <div v-for="(c, i) in replay.llm_calls" :key="i" class="ev-step">
            <span class="ev-step-name mono">{{ c.node }} · {{ c.model }}</span>
            <span class="ev-step-meta">{{ replayStepText(c) }}</span>
          </div>
        </div>
        <div v-if="replay.tools?.length" class="ev-replay">
          <div class="ev-replay-cap">{{ t('provReplayTools', lang) }}</div>
          <div v-for="(tool, i) in replay.tools" :key="i" class="ev-step">
            <span class="ev-step-name mono">{{ tool.name }}</span>
            <span class="ev-step-meta">{{ tool.node }}</span>
          </div>
        </div>
      </template>
    </section>
  </DetailDrawer>
</template>

<style scoped>
.ev-head {
  min-width: 0;
  flex: 1;
}
.ev-title {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-primary);
}
.ev-chips {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  margin-top: var(--sp-2);
}
.ev-chip {
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}

.ev-sec {
  margin-bottom: var(--sp-5);
}
.ev-sec:last-child {
  margin-bottom: 0;
}
.ev-h {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.ev-tag {
  font-weight: 400;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.ev-sqlbox {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  overflow: hidden;
  background: var(--surface-muted);
}
.ev-sqlbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-1) var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.ev-sql {
  margin: 0;
  padding: var(--sp-3);
  overflow-x: auto;
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--text-primary);
  white-space: pre;
}

.ev-mini {
  padding: 1px 7px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.ev-mini:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.ev-kv {
  display: grid;
  grid-template-columns: 92px 1fr;
  row-gap: var(--sp-2);
  column-gap: var(--sp-3);
  margin: 0;
}
.ev-kvrow {
  display: contents;
}
.ev-kvrow dt {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.ev-kvrow dd {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-xs);
  word-break: break-word;
}

.ev-tablewrap {
  overflow-x: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
}
.ev-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.ev-table th,
.ev-table td {
  padding: var(--sp-1) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  text-align: left;
  white-space: nowrap;
}
.ev-table th {
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-weight: 500;
}
.ev-table tr:last-child td {
  border-bottom: none;
}

.ev-check {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: var(--sp-1) 0;
  border-bottom: 1px dashed var(--border-subtle);
}
.ev-check:last-child {
  border-bottom: none;
}
.ev-check-main {
  font-size: var(--fs-xs);
  color: var(--text-primary);
}
.ev-check-sub {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.ev-fbrow {
  display: flex;
  gap: var(--sp-2);
}
.ev-fbbtn {
  padding: var(--sp-1) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.ev-fbbtn:hover {
  background: var(--surface-hover);
}
.ev-fbbtn.active {
  border-color: var(--indigo-400);
  background: var(--surface-accent);
  color: var(--indigo-700);
}

.ev-receipt {
  margin: var(--sp-2) 0 0;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  background: var(--ok-bg);
  color: var(--ok-text);
  font-size: var(--fs-2xs);
}
.ev-receipt.warn {
  background: var(--warn-bg);
  color: var(--warn-text);
}

.ev-note {
  margin: var(--sp-2) 0 0;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
  line-height: var(--lh-relaxed);
}

.ev-replay {
  margin-bottom: var(--sp-2);
}
.ev-replay-cap {
  margin-bottom: 2px;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.ev-step {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  border-bottom: 1px dashed var(--border-subtle);
}
.ev-step:last-child {
  border-bottom: none;
}
.ev-step-name {
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  word-break: break-all;
}
.ev-step-meta {
  flex: none;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
