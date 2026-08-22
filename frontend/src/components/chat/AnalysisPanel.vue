<template>
  <aside
    class="analysis-panel"
    :class="{ open: ui.analysisOpen }"
  >
    <header class="analysis-head">
      <span class="analysis-title">{{ t('analysisTitle', ui.lang) }}</span>
      <button class="topbar-btn" @click="ui.toggleAnalysis()">
        <el-icon :size="14"><Close /></el-icon>
      </button>
    </header>
    <div class="analysis-body">
      <div v-if="chat.tasks.length" class="analysis-tasks">
        <div class="analysis-section-title">{{ t('tasks', ui.lang) }}</div>
        <div v-for="task in chat.tasks" :key="task.task_id" class="task-row">
          <span class="task-mark" :class="task.status">
            <el-icon :size="14"
              ><component :is="taskIcon(task.status)"
            /></el-icon>
          </span>
          <span class="task-title">{{ task.title }}</span>
        </div>
      </div>
      <template v-if="currentTurn">
        <div
          v-for="(step, j) in currentTurn.steps"
          :key="j"
          class="step-wrap"
        >
          <StepCard :card="step" />
        </div>
        <div v-for="(th, k) in currentTurn.thoughts" :key="k" class="thoughts">
          <details>
            <summary>thought</summary>
            <div>{{ th }}</div>
          </details>
        </div>
      </template>
      <div v-else class="analysis-empty">{{ t('analysisEmpty', ui.lang) }}</div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  Close,
  CircleCheckFilled,
  CircleCloseFilled,
  Loading,
  CirclePlus,
} from '@element-plus/icons-vue'
import StepCard from './StepCard.vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const chat = useChatStore()
const ui = useUiStore()

const currentTurn = computed(() => chat.currentTurn)

function taskIcon(status: string) {
  switch (status) {
    case 'done':
      return CircleCheckFilled
    case 'failed':
      return CircleCloseFilled
    case 'in_progress':
      return Loading
    default:
      return CirclePlus
  }
}
</script>