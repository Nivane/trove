<template>
  <div v-if="chat.tasks.length" class="task-panel">
    <div class="task-panel-title">{{ t('tasks', ui.lang) }}</div>
    <div v-for="task in chat.tasks" :key="task.task_id" class="task-row">
      <span class="task-mark" :class="task.status">{{ mark(task.status) }}</span>
      <span class="task-title">{{ task.title }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const chat = useChatStore()
const ui = useUiStore()

function mark(status: string): string {
  switch (status) {
    case 'done': return '✓'
    case 'failed': return '✕'
    case 'in_progress': return '●'
    default: return '○'
  }
}
</script>
