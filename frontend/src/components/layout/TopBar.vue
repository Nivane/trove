<template>
  <header class="topbar">
    <button class="sidebar-toggle" @click="ui.toggleSidebar()">☰</button>
    <span class="session-title">{{ sessionLabel }}</span>
    <span class="topbar-spacer"></span>
    <span class="datasource-label">{{ datasource }}</span>
    <button class="lang-toggle" @click="toggleLang">{{ ui.lang === 'zh' ? 'EN' : '中' }}</button>
    <button class="theme-toggle" @click="ui.cycleTheme()">{{ ui.theme === 'dark' ? '☀' : '☾' }}</button>
  </header>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useUiStore } from '../../stores/ui'
import { useChatStore } from '../../stores/chat'
import { apiGet } from '../../api/http'

const ui = useUiStore()
const chat = useChatStore()
const datasource = ref('')
const sessionLabel = ref('')

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

// session title is derived from the first question (kept simple)
</script>
