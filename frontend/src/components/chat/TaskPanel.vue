<template>
  <div v-if="chat.tasks.length" class="task-panel">
    <div class="task-panel-title">{{ t('tasks', ui.lang) }}</div>
    <div v-for="task in chat.tasks" :key="task.task_id" class="task-row">
      <span class="task-mark" :class="task.status">
        <el-icon :size="14"><component :is="icon(task.status)" /></el-icon>
      </span>
      <span class="task-title">{{ task.title }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { CircleCheckFilled, CircleCloseFilled, Loading, CirclePlus } from '@element-plus/icons-vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const chat = useChatStore()
const ui = useUiStore()

function icon(status: string) {
  switch (status) {
    case 'done': return CircleCheckFilled
    case 'failed': return CircleCloseFilled
    case 'in_progress': return Loading
    default: return CirclePlus
  }
}
</script>
