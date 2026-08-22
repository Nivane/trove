<template>
  <div class="chat-shell">
    <Sidebar />
    <div class="chat-main">
      <div class="chat-col">
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
          <Composer />
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
              <div
                v-if="turn.answer || turn.synthesis"
                class="answer"
                :class="{ streaming: turn.status === 'streaming' }"
              >
                <MarkdownView
                :source="turn.answer || turn.synthesis || ''"
                :result-rows="turn.summary?.rows ?? null"
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
                />
              </div>
              <div
                v-if="turn.status === 'hitl' && !turn.hitlActionsShown"
                class="step-wrap"
              >
                <HitlCard :batch="!!turn.hitlBatch" />
              </div>
              <div v-if="turn.error" class="error-box">
                <span>{{ turn.error }}</span>
                <button class="retry-btn" @click="chat.retry()">
                  <RefreshRight :size="14" />
                  {{ t('retry', ui.lang) }}
                </button>
              </div>
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
            </div>
          </template>
        </div>
        <Composer class="composer-slot" />
      </template>
      </div>
      <AnalysisPanel />
    </div>
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
} from 'lucide-vue-next'
import { RefreshRight } from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import Sidebar from '../components/layout/Sidebar.vue'
import AnalysisPanel from '../components/chat/AnalysisPanel.vue'
import ChartCard from '../components/chat/ChartCard.vue'
import HitlCard from '../components/chat/HitlCard.vue'
import MarkdownView from '../components/chat/MarkdownView.vue'
import Composer from '../components/chat/Composer.vue'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { t } from '../i18n'
import { copyText } from '../utils/format'
import type { Turn } from '../stores/chat'

const chat = useChatStore()
const ui = useUiStore()
const messageList = ref<HTMLDivElement>()
const editingId = ref(-1)
const editDraft = ref('')
const editEls = ref<HTMLTextAreaElement[]>([])
const copiedId = ref(-1)
const userCopiedId = ref(-1)
const regenerateId = ref(-1)

const analysisToggleTitle = computed(() => t('analysisToggle', ui.lang))

function isLastTurn(i: number): boolean {
  return i === chat.turns.length - 1
}

async function rate(turn: Turn, vote: 1 | -1) {
  if (turn.rating === vote) return
  const index = chat.turns.indexOf(turn)
  if (index < 0) return
  await chat.rateTurn(index, vote)
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

onMounted(async () => {
  await chat.listSessions()
  if (chat.sessionId) await chat.loadTasks(chat.sessionId)
})
</script>