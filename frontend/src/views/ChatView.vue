<template>
  <div class="chat-shell">
    <Sidebar />
    <div class="chat-main">
      <TopBar />
      <TaskPanel />
      <div ref="messageList" class="message-list">
        <template v-if="!chat.turns.length">
          <Welcome />
        </template>
        <template v-for="(turn, i) in chat.turns" :key="i">
          <div class="user-bubble">{{ turn.question }}</div>
          <div class="assistant-turn">
            <div v-for="(step, j) in turn.steps" :key="j" class="step-wrap">
              <StepCard :card="step" />
            </div>
            <div v-if="turn.thoughts.length" class="thoughts">
              <details v-for="(th, k) in turn.thoughts" :key="k">
                <summary>thought</summary>
                <div>{{ th }}</div>
              </details>
            </div>
            <div v-if="turn.summary?.sql" class="summary-sql">
              <pre class="code-block sql"><code>{{ turn.summary.sql }}</code></pre>
            </div>
            <div v-if="turn.summary?.chart_option || turn.summary?.chart" class="chart-wrap">
              <ChartCard :chart="turn.summary.chart" :option="turn.summary.chart_option" />
            </div>
            <div v-if="turn.answer || turn.summary?.final_response" class="answer">
              <MarkdownView :source="turn.answer || turn.summary!.final_response!" />
            </div>
            <div v-if="turn.status === 'hitl' && !turn.hitlActionsShown" class="step-wrap">
              <HitlCard :batch="!!turn.hitlBatch" />
            </div>
            <div v-if="turn.error" class="error-box">{{ turn.error }}</div>
            <div v-if="turn.status === 'streaming'" class="streaming-dots"><span></span><span></span><span></span></div>
          </div>
        </template>
      </div>
      <Composer class="composer-slot" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onMounted } from 'vue'
import Sidebar from '../components/layout/Sidebar.vue'
import TopBar from '../components/layout/TopBar.vue'
import TaskPanel from '../components/chat/TaskPanel.vue'
import StepCard from '../components/chat/StepCard.vue'
import ChartCard from '../components/chat/ChartCard.vue'
import HitlCard from '../components/chat/HitlCard.vue'
import MarkdownView from '../components/chat/MarkdownView.vue'
import Composer from '../components/chat/Composer.vue'
import Welcome from '../components/chat/Welcome.vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const messageList = ref<HTMLDivElement>()

function scrollToBottom() {
  void nextTick(() => {
    const el = messageList.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(
  () => chat.turns.map((t) => t.answer.length + t.steps.length + t.thoughts.length).join(','),
  scrollToBottom,
)

onMounted(async () => {
  await chat.listSessions()
  if (chat.sessionId) await chat.loadTasks(chat.sessionId)
})
</script>
