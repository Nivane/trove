<template>
  <aside
    class="analysis-panel"
    :class="{ open: ui.analysisOpen }"
  >
    <header class="analysis-head">
      <span class="analysis-title">
        {{ t('analysisTitle', ui.lang) }}
        <span v-if="headStats" class="analysis-head-stats">{{ headStats }}</span>
      </span>
      <button class="topbar-btn" @click="ui.toggleAnalysis()">
        <X :size="14" />
      </button>
    </header>
    <div ref="panelBody" class="analysis-body">
      <div v-if="chat.tasks.length" class="analysis-tasks">
        <div class="analysis-section-title">{{ t('tasks', ui.lang) }}</div>
        <div v-for="task in chat.tasks" :key="task.task_id" class="task-row">
          <span class="task-mark" :class="task.status">
            <component
              :is="taskIcon(task.status)"
              :size="14"
              :class="{ spin: task.status === 'in_progress' }"
            />
          </span>
          <span class="task-title">{{ task.title }}</span>
        </div>
      </div>
      <template v-if="currentTurn">
        <!-- ── Live status bar: which node is running & for how long ── -->
        <div v-if="liveBarVis" class="analysis-status" :class="statusClass">
          <template v-if="currentTurn.status === 'streaming' && liveNode">
            <LoaderCircle :size="15" class="spin status-spinner" />
            <div class="status-text">
              <div class="status-line">
                <span class="status-k">{{ t('runningStep', ui.lang) }}</span>
                <span class="status-node">{{ liveLabel }}</span>
                <span class="status-elapsed">{{ fmtMs(liveMs) }}</span>
              </div>
              <div v-if="stuck" class="status-hint">
                {{ t('stuckHint', ui.lang) }}
              </div>
            </div>
          </template>
          <template v-else-if="currentTurn.status === 'streaming'">
            <LoaderCircle :size="15" class="spin status-spinner" />
            <div class="status-text">
              <div class="status-line">
                <span class="status-k">{{ t('runningNow', ui.lang) }}</span>
                <span class="status-elapsed">{{ fmtMs(totalMs) }}</span>
              </div>
            </div>
          </template>
          <template v-else-if="currentTurn.status === 'error'">
            <CircleX :size="15" class="status-err-icon" />
            <div class="status-text">
              <div class="status-line">
                <span class="status-k">{{ t('failedStep', ui.lang) }}</span>
                <span class="status-node">{{ lastFailedLabel || t('error', ui.lang) }}</span>
              </div>
              <div v-if="failureTitle" class="status-hint">{{ failureTitle }}</div>
            </div>
          </template>
          <template v-else-if="currentTurn.status === 'done' && currentTurn.steps.length">
            <CircleCheck :size="15" class="status-ok-icon" />
            <div class="status-text">
              <div class="status-line">
                <span>{{ t('stepsCount', ui.lang, currentTurn.steps.length) }}</span>
                <span v-if="summaryTotal != null" class="status-elapsed">{{
                  fmtMs(summaryTotal)
                }}</span>
              </div>
            </div>
          </template>
        </div>

        <!-- ── Completed steps timeline ── -->
        <div
          v-for="(step, j) in currentTurn.steps"
          :key="j"
          class="step-wrap"
        >
          <StepCard :card="step" :attempt="stepAttempt(j)" />
        </div>

        <!-- ── In-flight nodes (begin events not yet resolved) ── -->
        <div
          v-for="ls in currentTurn.live ?? []"
          :key="`live-${ls.startedSeq}`"
          class="step-wrap"
        >
          <StepCard
            :card="{ node: ls.node, label: ls.label, payload: { node: ls.node } }"
            status="running"
            :live-ms="Math.max(0, now - ls.startedAt)"
          />
        </div>

        <div
          v-for="(th, k) in currentTurn.thoughts"
          :key="`th-${k}`"
          class="thought-card"
        >
          <details>
            <summary>
              <span>{{ t('thought', ui.lang) }}</span>
              <span class="thought-idx">{{ k + 1 }}</span>
            </summary>
            <div class="thought-body">{{ th }}</div>
          </details>
        </div>

        <!-- ① 截断写明:上限之外还有 N 步没随历史保存(不静默丢步)。 -->
        <div v-if="currentTurn.stepsTruncated" class="analysis-truncated">
          {{ t('analysisStepsTruncated', ui.lang, currentTurn.stepsTruncated) }}
        </div>
        <!-- 零步骤的完结轮(缓存命中 / 旧会话 / 中断轮):不留纯空白。 -->
        <div v-if="emptyStepsNote" class="analysis-empty analysis-empty-turn">
          {{ t('analysisNoSteps', ui.lang) }}
        </div>
      </template>
      <div v-else class="analysis-empty">{{ t('analysisEmpty', ui.lang) }}</div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, nextTick } from 'vue'
import { CircleCheck, CirclePlus, CircleX, LoaderCircle, X } from 'lucide-vue-next'
import StepCard from './StepCard.vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { stepLabel, fmtMs } from '../../utils/steps'
import { errorCard } from '../../utils/errors'

const chat = useChatStore()
const ui = useUiStore()

const now = ref(Date.now())
const panelBody = ref<HTMLDivElement>()
const STUCK_MS = 90_000

let timer: number | undefined
function startTimer() {
  stopTimer()
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 500)
}
function stopTimer() {
  if (timer != null) {
    window.clearInterval(timer)
    timer = undefined
  }
}
onBeforeUnmount(() => stopTimer())

// Run the live elapsed meter only while a turn is actually streaming —
// once it finishes, freeze the clock (done turns show the backend's
// summary.total_elapsed_ms instead of a runaway counter).
watch(
  () => chat.currentTurn?.status ?? '',
  (s) => {
    if (s === 'streaming') startTimer()
    else stopTimer()
  },
  { immediate: true },
)

const currentTurn = computed(() => chat.currentTurn)

const totalMs = computed(() => {
  const t = currentTurn.value
  if (!t?.startedAt) return 0
  return Math.max(0, now.value - t.startedAt)
})

const liveNode = computed(() => {
  const t = currentTurn.value
  if (!t?.live?.length) return null
  return t.live[t.live.length - 1]
})

const liveMs = computed(() => {
  const lv = liveNode.value
  return lv ? Math.max(0, now.value - lv.startedAt) : 0
})

const stuck = computed(() => liveMs.value > STUCK_MS)

const liveLabel = computed(() => {
  const lv = liveNode.value
  if (!lv) return ''
  return lv.label || stepLabel(lv.node, ui.lang)
})

const liveBarVis = computed(() => {
  const t = currentTurn.value
  if (!t) return false
  if (t.status === 'streaming') return true
  if (t.status === 'error') return true
  if (t.status === 'done' && t.steps.length) return true
  return false
})

const statusClass = computed(() => {
  const t = currentTurn.value
  if (!t) return ''
  if (t.status === 'error') return 'status-error'
  if (t.status === 'done') return 'status-done'
  return stuck.value ? 'status-stuck' : 'status-live'
})

const lastFailedLabel = computed(() => {
  const t = currentTurn.value
  if (!t?.steps.length) return ''
  const last = t.steps[t.steps.length - 1]
  return last.label || stepLabel(last.node, ui.lang)
})

/** 失败原因一句话:错误卡片同一份文案,面板不另起一套措辞。
 *  结构化错误用短标题(状态栏要的是"发生了什么");老会话只有原始串,
 *  那就照实显示 —— 总比什么都不说强。 */
const failureTitle = computed(() => {
  const t = currentTurn.value
  if (!t) return ''
  const card = errorCard({ error: t.error, error_info: t.errorInfo }, ui.lang)
  if (t.errorInfo) return card?.title || ''
  return card?.explanation || card?.title || t.error || ''
})

const summaryTotal = computed(() => {
  const t = currentTurn.value
  const ms = t?.summary?.total_elapsed_ms
  return typeof ms === 'number' ? ms : null
})

/** Header/status elapsed: live while streaming, backend total once done. */
const headTotal = computed(() => {
  const t = currentTurn.value
  if (!t?.startedAt) return 0
  const ms = t.summary?.total_elapsed_ms
  if (t.status === 'done' && typeof ms === 'number') return ms
  return Math.max(0, now.value - t.startedAt)
})

/** Header stats: 直播轮 = 已用时 · N 步;历史轮没有直播时钟(startedAt
 *  缺席),只给 N 步 —— 有几步说几步,不补零。 */
const headStats = computed(() => {
  const turn = currentTurn.value
  if (!turn) return ''
  const parts: string[] = []
  if (turn.startedAt) parts.push(fmtMs(headTotal.value))
  if (turn.steps.length) parts.push(t('stepsCount', ui.lang, turn.steps.length))
  return parts.join(' · ')
})

/** 零步骤的完结轮(缓存命中 / 旧会话 / 中断轮)—— 面板不留纯空白,明说
 *  「本轮无步骤记录」。直播中(status=streaming)不插话:状态条已在说明。 */
const emptyStepsNote = computed(() => {
  const turn = currentTurn.value
  if (!turn || turn.status === 'streaming') return false
  return !turn.steps.length && !turn.live?.length && !turn.thoughts.length
})

function stepAttempt(j: number): number {
  const t = currentTurn.value
  if (!t) return 0
  const node = t.steps[j]?.node
  if (!node) return 0
  let count = 0
  for (let i = 0; i <= j; i++) {
    if (t.steps[i].node === node) count++
  }
  return count
}

function scrollToBottom() {
  void nextTick(() => {
    const el = panelBody.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(
  () =>
    chat.currentTurn?.steps.length + ',' + (chat.currentTurn?.live?.length ?? 0),
  scrollToBottom,
)

function taskIcon(status: string) {
  switch (status) {
    case 'done':
      return CircleCheck
    case 'failed':
      return CircleX
    case 'in_progress':
      return LoaderCircle
    default:
      return CirclePlus
  }
}
</script>