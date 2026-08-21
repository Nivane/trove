<template>
  <header class="topbar">
    <button class="topbar-btn" @click="ui.toggleSidebar()"><el-icon :size="16"><Expand /></el-icon></button>
    <span class="session-title">{{ sessionLabel }}</span>
    <span class="topbar-spacer"></span>
    <span class="datasource-label">
      <el-icon :size="12"><Odometer /></el-icon>
      {{ datasource || t('datasources', ui.lang) }}
    </span>
    <button class="topbar-btn" :title="langTitle" @click="toggleLang">中/EN</button>
    <button class="topbar-btn" :title="themeTitle" @click="ui.cycleTheme()">
      <el-icon :size="16"><component :is="ui.theme === 'dark' ? Sunny : Moon" /></el-icon>
    </button>
  </header>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { Expand, Odometer, Sunny, Moon } from '@element-plus/icons-vue'
import { useUiStore } from '../../stores/ui'
import { useChatStore } from '../../stores/chat'
import { t } from '../../i18n'
import { apiGet } from '../../api/http'

const ui = useUiStore()
const chat = useChatStore()
const datasource = ref('')

const sessionLabel = computed(() => {
  const first = chat.turns[0]?.question
  return first ? first.slice(0, 40) : chat.sessionId.slice(0, 8)
})
const langTitle = computed(() => (ui.lang === 'zh' ? 'English' : '简体中文'))
const themeTitle = computed(() => (ui.theme === 'dark' ? t('themeLight', ui.lang) : t('themeDark', ui.lang)))

function toggleLang() {
  ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
  window.location.reload()
}

onMounted(async () => {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    const list = body.datasources ?? []
    datasource.value = list[0]?.name ?? ''
  } catch {
    datasource.value = ''
  }
})
</script>
