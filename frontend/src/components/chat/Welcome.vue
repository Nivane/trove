<template>
  <div class="welcome">
    <div class="welcome-mark"><el-icon :size="44"><DataAnalysis /></el-icon></div>
    <h1 class="welcome-title">{{ t('welcomeTitle', ui.lang) }}</h1>
    <p class="welcome-subtitle">{{ t('welcomeSubtitle', ui.lang) }}</p>
    <div class="welcome-examples">
      <button
        v-for="ex in examples"
        :key="ex"
        class="example-chip"
        :disabled="chat.streaming"
        @click="ask(ex)"
      >
        {{ ex }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { DataAnalysis } from '@element-plus/icons-vue'
import { useUiStore } from '../../stores/ui'
import { useChatStore } from '../../stores/chat'
import { t } from '../../i18n'

const ui = useUiStore()
const chat = useChatStore()

const examples = computed(() => [
  t('example1', ui.lang),
  t('example2', ui.lang),
  t('example3', ui.lang),
])

async function ask(q: string) {
  await chat.send(q)
}
</script>
