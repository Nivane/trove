<template>
  <div class="chat-shell">
    <Sidebar />
    <div class="chat-main">
      <div class="chat-col">
        <div v-if="ui.datasourceList.length" class="chat-ds-bar">
          <div class="chat-ds-label">
            <Database :size="14" :stroke-width="2" />
            <span>{{ t('datasource', ui.lang) }}</span>
          </div>
          <el-select
            :model-value="ui.activeDatasource"
            class="chat-ds-select"
            size="small"
            :placeholder="t('dsSelectPlaceholder', ui.lang)"
            @change="onDatasourceChange"
          >
            <el-option
              v-for="ds in ui.datasourceList"
              :key="ds.name"
              :label="ds.name"
              :value="ds.name"
            >
              <span class="ds-opt-name">{{ ds.name }}</span>
              <span class="ds-opt-type">{{ dsTypeLabel(ds.type) }}</span>
              <span v-if="ds.default" class="chat-ds-default">
                {{ t('dsDefault', ui.lang) }}
              </span>
            </el-option>
          </el-select>
          <span
            v-if="!ui.hasDatasource && ui.datasource"
            class="chat-ds-fallback"
          >
            {{ t('dsFallback', ui.lang) }}
          </span>
          <!-- 主题域:当前源声明了域才出现(内部自持 v-if,免得这里再判断)。 -->
          <TopicSelect />
        </div>
        <button
          v-if="chat.turns.length"
          class="analysis-toggle"
          :class="{ active: ui.analysisOpen }"
          :title="analysisToggleTitle"
          @click="ui.toggleAnalysis()"
        >
          <component
            :is="ui.analysisOpen ? PanelRightClose : PanelRightOpen"
            :size="16"
          />
        </button>
        <div
          v-if="ui.datasourcesLoaded && !ui.datasourceList.length"
          class="no-ds-banner"
        >
          {{ t('noDatasources', ui.lang) }}
        </div>
        <div v-if="!chat.turns.length" class="empty-center">
          <Composer ref="emptyComposer" />
          <div v-if="ui.datasourceList.length" class="empty-examples">
            <div class="empty-examples-title">{{ starterTitle }}</div>
            <button
              v-for="ex in starterExamples"
              :key="ex"
              class="empty-example-btn"
              @click="fillExample(ex)"
            >
              {{ ex }}
            </button>
          </div>
        </div>
      <template v-else>
        <div ref="messageList" class="message-list">
          <template v-for="(turn, i) in chat.turns" :key="i">
            <div
              class="user-bubble-wrap"
              :class="{ editing: editingId === i }"
            >
              <div v-if="editingId !== i" class="user-bubble">
                {{ turn.question }}
              </div>
              <form v-else class="user-edit" @submit.prevent="commitEdit(i)">
                <textarea
                  ref="editEls"
                  v-model="editDraft"
                  class="user-edit-input"
                  rows="1"
                  @keydown="onEditKeydown($event, i)"
                  @input="autoGrowEdit"
                />
                <div class="edit-toolbar">
                  <span class="edit-toolbar-spacer" />
                  <button
                    type="button"
                    class="edit-tool-btn"
                    :title="t('cancel', ui.lang)"
                    @click="editingId = -1"
                  >
                    <X :size="16" />
                  </button>
                  <button
                    type="submit"
                    class="edit-tool-btn primary"
                    :disabled="!editDraft.trim()"
                    :title="t('send', ui.lang)"
                  >
                    <ArrowUp :size="16" />
                  </button>
                </div>
              </form>
              <button
                v-if="editingId !== i && !chat.streaming"
                class="edit-pencil copy"
                :title="t('copy', ui.lang)"
                @click="copyQuestion(turn, i)"
              >
                <Check v-if="userCopiedId === i" :size="14" :stroke-width="2" />
                <Copy v-else :size="14" :stroke-width="2" />
              </button>
              <button
                v-if="editingId !== i && !chat.streaming"
                class="edit-pencil"
                :title="t('edit', ui.lang)"
                @click="startEdit(i)"
              >
                <Pencil :size="14" :stroke-width="2" />
              </button>
            </div>
            <div class="assistant-turn">
              <div v-if="turn.summary?.rewritten_question && turn.summary.rewritten_question !== turn.question" class="rewrite-note">
                {{ t('rewriteNote', ui.lang) }}<span class="rewrite-note-q">{{ turn.summary.rewritten_question }}</span>
              </div>
              <div
                v-if="turn.summary?.datasource || maskingBadge(turn.summary?.masking_applied)"
                class="answer-meta"
              >
                <span v-if="turn.summary?.datasource" class="ds-badge" :title="t('dsBadge', ui.lang)">
                  <Database :size="12" :stroke-width="2" />
                  {{ turn.summary.datasource }}
                </span>
                <!-- 脱敏提示:数据被改写了而改写本身无声,不说一句就会被读成真实值。
                     两态分开显示 —— 「已脱敏 N 字段」与「本次原文」(bypass 的
                     fields 是空的,却恰恰是最该说一句的那次)。 -->
                <span
                  v-if="maskingBadge(turn.summary?.masking_applied)"
                  class="mask-badge"
                  :class="maskingBadge(turn.summary?.masking_applied)?.kind"
                  :title="
                    maskingBadge(turn.summary?.masking_applied)?.kind === 'bypass'
                      ? t('bypassBadgeTip', ui.lang)
                      : t('maskedColTip', ui.lang)
                  "
                >
                  <Lock :size="12" :stroke-width="2" />
                  {{ maskLabel(turn.summary?.masking_applied) }}
                </span>
              </div>
              <div
                v-if="(turn.answer || turn.synthesis) && !cards[i]"
                class="answer"
                :class="{ streaming: turn.status === 'streaming' }"
              >
                <MarkdownView
                :source="turn.answer || turn.synthesis || ''"
                :result-rows="turn.summary?.rows ?? null"
                :masking="turn.summary?.masking_applied ?? null"
              />
                <span
                  v-if="turn.status === 'streaming'"
                  class="stream-caret"
                />
              </div>
              <div
                v-if="
                  turn.status === 'streaming' &&
                  !turn.answer &&
                  !turn.synthesis
                "
                class="streaming-badge"
              >
                <LoaderCircle :size="13" class="spin" />
                <span>{{ t('generating', ui.lang) }}</span>
              </div>
              <div
                v-if="turn.summary?.chart_option || turn.summary?.chart"
                class="chart-wrap"
              >
                <ChartCard
                  :chart="turn.summary.chart"
                  :option="turn.summary.chart_option"
                  @ask="askChartFollowUp"
                />
              </div>
              <!-- 分析卡(分析柱):结构化归因 —— 瀑布/贡献表/驱动树/证据。
                   与答案 markdown 里的分析区块同源;没有分析就不渲染。 -->
              <AnalysisCard
                v-if="turn.status === 'done' && turn.summary?.analysis"
                :analysis="turn.summary.analysis"
              />
              <div
                v-if="turn.status === 'hitl' && !turn.hitlActionsShown"
                class="step-wrap"
              >
                <HitlCard :batch="!!turn.hitlBatch" />
              </div>
              <!-- 未完成轮(方案 ⑤):提问已落盘、答案没落盘 —— 仍在跑或
                   已中断。灰字如实标注,不装作正常收官。 -->
              <div v-if="turn.unfinished" class="unfinished-note">
                {{ t('turnUnfinished', ui.lang) }}
              </div>
              <ErrorCard
                v-if="cards[i]"
                :card="cards[i]!"
                @retry="chat.retry()"
                @rephrase="rephraseLast(i)"
                @admin="gotoAdmin"
              />
              <!-- 溯源条:答案自带身份(数据源·时间·模型·run_id + 状态 chip),
                   默认一行,点开 12 行明细。拿不到就不显示(P1 验收第 3 条)。 -->
              <ProvenanceStrip
                v-if="turn.status === 'done' && turn.summary"
                :summary="turn.summary"
                :at="turn.at"
                :lang="ui.lang"
                @open-evidence="openEvidence(i, 'evidence')"
                @open-replay="openEvidence(i, 'replay')"
              />
              <div v-if="turn.status === 'done'" class="rating-row">
                <button
                  class="rate-btn"
                  :title="t('copy', ui.lang)"
                  @click="copyAnswer(turn)"
                >
                  <Check v-if="copiedId === i" :size="14" />
                  <Copy v-else :size="14" />
                </button>
                <button
                  v-if="isLastTurn(i)"
                  class="rate-btn"
                  :title="t('regenerate', ui.lang)"
                  @click="askRegenerate(i)"
                >
                  <RotateCcw :size="14" />
                </button>
                <button
                  class="rate-btn evidence-cta"
                  :title="t('provOpenEvidence', ui.lang)"
                  @click="openEvidence(i, 'evidence')"
                >
                  <Search :size="14" />
                  <span>{{ t('provOpenEvidence', ui.lang) }}</span>
                </button>
                <span class="rating-sep" />
                <button
                  class="rate-btn"
                  :class="{ active: turn.rating === 1 }"
                  :title="t('thumbUp', ui.lang)"
                  @click="rate(turn, 1)"
                >
                  <ThumbsUp :size="14" />
                </button>
                <button
                  class="rate-btn"
                  :class="{ active: turn.rating === -1 }"
                  :title="t('thumbDown', ui.lang)"
                  @click="rate(turn, -1)"
                >
                  <ThumbsDown :size="14" />
                </button>
              </div>
              <div v-if="ratingReasonsFor === i" class="rating-reasons">
                <div class="rating-reasons-title">{{ t('ratingReasonsTitle', ui.lang) }}</div>
                <div class="rating-reasons-list">
                  <button
                    v-for="r in ratingReasons"
                    :key="r.key"
                    class="rating-reason-btn"
                    @click="rateWithReason(i, r.key)"
                  >
                    {{ r.label }}
                  </button>
                </div>
              </div>
              <div v-if="regenerateId === i" class="regenerate-confirm">
                <span class="regenerate-confirm-text">{{
                  t('regenerateConfirm', ui.lang)
                }}</span>
                <button class="mini-btn confirm" @click="doRegenerate()">
                  {{ t('confirm', ui.lang) }}
                </button>
                <button class="mini-btn" @click="regenerateId = -1">
                  {{ t('cancel', ui.lang) }}
                </button>
              </div>
              <div v-if="receiptFor === i" class="rating-receipt">
                {{ receiptText }}
              </div>
            </div>
          </template>
        </div>
        <Composer class="composer-slot" />
      </template>
      </div>
      <AnalysisPanel />
    </div>
    <!-- 依据抽屉(View evidence):SQL / 执行要点 / 结果集预览 / 校验与反思 /
         反馈入口 / 只读回放。按轮下标引用,内容只读已落盘的 summary。 -->
    <EvidenceDrawer
      v-model="drawerOpen"
      :turn="drawerTurn"
      :turn-index="drawerIndex"
      :focus="drawerFocus"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import {
  PanelRightClose,
  PanelRightOpen,
  Copy,
  Check,
  RotateCcw,
  LoaderCircle,
  ThumbsUp,
  ThumbsDown,
  Pencil,
  ArrowUp,
  X,
  Database,
  Lock,
  Search,
} from 'lucide-vue-next'
import { ElMessageBox } from 'element-plus'
import Sidebar from '../components/layout/Sidebar.vue'
import AnalysisCard from '../components/chat/AnalysisCard.vue'
import AnalysisPanel from '../components/chat/AnalysisPanel.vue'
import ChartCard from '../components/chat/ChartCard.vue'
import ErrorCard from '../components/chat/ErrorCard.vue'
import EvidenceDrawer from '../components/chat/EvidenceDrawer.vue'
import HitlCard from '../components/chat/HitlCard.vue'
import MarkdownView from '../components/chat/MarkdownView.vue'
import ProvenanceStrip from '../components/chat/ProvenanceStrip.vue'
import { maskingBadge } from '../utils/masking'
import type { MaskingReport } from '../utils/masking'
import Composer from '../components/chat/Composer.vue'
import TopicSelect from '../components/chat/TopicSelect.vue'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { router } from '../router'
import { t } from '../i18n'
import { copyText, dsTypeLabel } from '../utils/format'
import { errorCard } from '../utils/errors'
import type { Turn } from '../stores/chat'

const chat = useChatStore()
const ui = useUiStore()

/** 脱敏徽标的文案。bypass 不走「已脱敏 N 字段」—— 那次 field 是空的,
 *  写出来会读成「本次没脱敏」,而事实是**以原文返回**。 */
function maskLabel(report: MaskingReport | null | undefined): string {
  const badge = maskingBadge(report)
  if (!badge) return ''
  return badge.kind === 'bypass'
    ? t('bypassBadge', ui.lang)
    : `${t('maskedBadge', ui.lang)} ${badge.count}`
}
const messageList = ref<HTMLDivElement>()
const emptyComposer = ref<InstanceType<typeof Composer> | null>(null)
const editingId = ref(-1)
const editDraft = ref('')
const editEls = ref<HTMLTextAreaElement[]>([])
const copiedId = ref(-1)
const userCopiedId = ref(-1)
const regenerateId = ref(-1)
const ratingReasonsFor = ref(-1)
// 依据抽屉(P1):按轮下标引用,内容全部来自该轮已落盘的 summary。
const drawerOpen = ref(false)
const drawerIndex = ref(-1)
const drawerFocus = ref<'evidence' | 'replay'>('evidence')
const drawerTurn = computed(() =>
  drawerIndex.value >= 0 ? (chat.turns[drawerIndex.value] ?? null) : null,
)
// 评分回执:提交成功才出现,文案如实说出去向(不承诺「已采纳」)。
const receiptFor = ref(-1)
const receiptText = ref('')

function openEvidence(i: number, focus: 'evidence' | 'replay' = 'evidence') {
  drawerIndex.value = i
  drawerFocus.value = focus
  drawerOpen.value = true
}

function showReceipt(i: number, vote: 1 | -1) {
  receiptFor.value = i
  receiptText.value = t(vote === 1 ? 'provFbReceiptUp' : 'provFbReceiptDown', ui.lang)
  window.setTimeout(() => {
    if (receiptFor.value === i) receiptFor.value = -1
  }, 6000)
}

const analysisToggleTitle = computed(() => t('analysisToggle', ui.lang))

// 失败轮次的错误卡片:后端给结构化 error_info 就照它渲染,没给(老会话)
// 退回脚手架文案 —— 两者都不解析错误 markdown。
const cards = computed(() =>
  chat.turns.map((turn) =>
    errorCard({ error: turn.error, error_info: turn.errorInfo }, ui.lang),
  ),
)

function onDatasourceChange(name: string) {
  ui.setDatasource(name)
}

const ratingReasons = computed(() => [
  { key: 'filter', label: t('ratingReasonFilter', ui.lang) },
  { key: 'value', label: t('ratingReasonValue', ui.lang) },
  { key: 'chart', label: t('ratingReasonChart', ui.lang) },
  { key: 'off', label: t('ratingReasonOff', ui.lang) },
])

const exampleQuestions = computed(() => [
  t('example1', ui.lang),
  t('example2', ui.lang),
  t('example3', ui.lang),
])

/** 当前生效的主题域对象(名字已在 activeTopic 里校验过)。 */
const selectedTopic = computed(
  () => ui.activeTopics.find((tp) => tp.name === ui.activeTopic) ?? null,
)

/** 起始提问:选中域且有示例 → 换成该域的问句(展示即 KB 原文);
 *  否则退回通用示例。 */
const starterExamples = computed(() => {
  const ex = selectedTopic.value?.examples ?? []
  return ex.length ? ex : exampleQuestions.value
})

const starterTitle = computed(() =>
  t(selectedTopic.value ? 'topicStarters' : 'examples', ui.lang),
)

async function fillExample(q: string) {
  // 填入输入框让用户确认/修改后自己发送(不直接提交)
  emptyComposer.value?.fillDraft(q)
}

/** 图表下钻追问:发给后端,复用省略式追问补全路由(指代词+历史)。 */
async function askChartFollowUp(q: string) {
  if (chat.streaming || !q.trim()) return
  await chat.send(q.trim())
}

function isLastTurn(i: number): boolean {
  return i === chat.turns.length - 1
}

async function rate(turn: Turn, vote: 1 | -1) {
  if (turn.rating === vote) return
  const index = chat.turns.indexOf(turn)
  if (index < 0) return
  if (vote === -1) {
    // 点踩 → 先弹出原因标签,不立即提交
    ratingReasonsFor.value = ratingReasonsFor.value === index ? -1 : index
    return
  }
  ratingReasonsFor.value = -1
  if (await chat.rateTurn(index, vote)) showReceipt(index, vote)
}

async function rateWithReason(index: number, reasonKey: string) {
  const reason = ratingReasons.value.find((r) => r.key === reasonKey)?.label
  ratingReasonsFor.value = -1
  if (await chat.rateTurn(index, -1, reason)) showReceipt(index, -1)
}

function rephraseLast(i: number) {
  // 换问法 = 直接编辑当前问题的气泡重新发送
  startEdit(i)
}

function gotoAdmin() {
  void router.push('/admin')
}

function askRegenerate(i: number) {
  regenerateId.value = regenerateId.value === i ? -1 : i
}

async function doRegenerate() {
  regenerateId.value = -1
  editingId.value = -1
  await chat.regenerate()
}

function startEdit(i: number) {
  const turn = chat.turns[i]
  if (!turn || chat.streaming) return
  editingId.value = i
  editDraft.value = turn.question
  void nextTick(() => {
    const el = editEls.value[i]
    if (el) {
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
      autoGrowEdit()
    }
  })
}

function onEditKeydown(e: KeyboardEvent, i: number) {
  if (e.isComposing) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    commitEdit(i)
  } else if (e.key === 'Escape') {
    editingId.value = -1
  }
}

function autoGrowEdit() {
  const i = editingId.value
  const el = i >= 0 ? editEls.value[i] : undefined
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

async function commitEdit(i: number) {
  if (editingId.value !== i) return
  const q = editDraft.value.trim()
  if (!q || chat.streaming) return
  const truncating = i < chat.turns.length - 1
  try {
    await ElMessageBox.confirm(
      truncating
        ? t('editTruncateConfirm', ui.lang, chat.turns.length - 1 - i)
        : t('editResendConfirm', ui.lang),
      '',
      {
        type: 'warning',
        confirmButtonText: t('confirm', ui.lang),
        cancelButtonText: t('cancel', ui.lang),
        roundButton: true,
      },
    )
  } catch {
    // cancelled — stay in edit mode so the draft is kept
    return
  }
  editingId.value = -1
  await chat.editAndResend(i, q)
}

async function copyAnswer(turn: Turn) {
  const text = turn.synthesis || turn.answer
  if (!text) return
  const ok = await copyText(text)
  const idx = chat.turns.indexOf(turn)
  if (!ok) return
  copiedId.value = idx
  window.setTimeout(() => {
    if (copiedId.value === idx) copiedId.value = -1
  }, 1600)
}

async function copyQuestion(turn: Turn, i: number) {
  const ok = await copyText(turn.question)
  if (!ok) return
  userCopiedId.value = i
  window.setTimeout(() => {
    if (userCopiedId.value === i) userCopiedId.value = -1
  }, 1600)
}

function scrollToBottom() {
  void nextTick(() => {
    const el = messageList.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(() => chat.turns.map((t) => t.answer.length).join(','), scrollToBottom)

// 数据源就绪/切换(含会话恢复时的源变化)→ 重拉该源的主题域清单。
// 「换源重置选择」不在这里做:那一步由 ui.setDatasource 统一负责 ——
// 选择重置必须发生在源变化的同一个动作里,而不是仰赖某个组件在场。
watch(
  () => ui.activeDatasource,
  (ds) => {
    void ui.loadTopics(ds)
  },
  { immediate: true },
)

onMounted(async () => {
  // 进入对话页 = 一次新的待输入对话:不自动选中/还原上一次会话
  // (此前 localStorage 里的 sessionId 会让侧栏高亮最近会话,但 turns 未
  // 加载 → 右侧空白、选中态与实际内容不一致)
  chat.clearSession()
  await chat.listSessions()
})
</script>

<style scoped>
/* 「查看依据」入口:在图标行里的一枚带字按钮(P1 的可点承诺)。 */
.rate-btn.evidence-cta {
  display: inline-flex;
  width: auto;
  gap: 4px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: var(--fs-2xs);
}
.rate-btn.evidence-cta:hover {
  border-color: var(--indigo-100);
  background: var(--surface-accent);
  color: var(--indigo-600);
}

/* 评分回执:提交成功才出现,如实说出去向(不承诺「已采纳」)。 */
.rating-receipt {
  margin-top: var(--sp-1);
  padding: 0 var(--sp-1);
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
}
</style>