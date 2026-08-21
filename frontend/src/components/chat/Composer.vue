<template>
  <form class="composer" @submit.prevent="submit">
    <div class="composer-box">
      <textarea
        v-model="draft"
        ref="inputEl"
        class="composer-input"
        :placeholder="t('placeholder', ui.lang)"
        rows="1"
        @keydown="onKeydown"
        @compositionstart="composing = true"
        @compositionend="composing = false"
        @input="onInput"
      ></textarea>
      <button
        v-if="!chat.streaming"
        class="send-btn"
        type="submit"
        :disabled="!draft.trim()"
      >
        <el-icon v-if="draft.trim()" :size="14"><Promotion /></el-icon>
        {{ t('send', ui.lang) }}
      </button>
      <button v-else class="stop-btn" type="button" @click="chat.stop()">
        <el-icon :size="14"><VideoPause /></el-icon>
        {{ t('stop', ui.lang) }}
      </button>
    </div>

    <div v-if="menuOpen" class="command-menu" role="listbox">
      <div class="command-menu-title">{{ t('slashHelp', ui.lang) }}</div>
      <button
        v-for="cmd in commands"
        :key="cmd.key"
        class="command-item"
        :class="{ active: cmd.key === activeCmd }"
        role="option"
        :aria-selected="cmd.key === activeCmd"
        @mousedown.prevent="runCommand(cmd)"
      >
        <span class="command-key">/{{ cmd.key }}</span>
        <span class="command-desc">{{ cmd.label }}</span>
      </button>
    </div>

    <div class="composer-hint">{{ t('composerHint', ui.lang) }}</div>
  </form>
</template>

<script setup lang="ts">
import { ref, nextTick, computed, onMounted, onUnmounted } from 'vue'
import { Promotion, VideoPause } from '@element-plus/icons-vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const chat = useChatStore()
const ui = useUiStore()
const draft = ref('')
const inputEl = ref<HTMLTextAreaElement>()
const composing = ref(false)

const menuOpen = ref(false)
const activeCmd = ref('clear')

interface Command {
  key: string
  label: string
}

const commands = computed<Command[]>(() => [
  { key: 'clear', label: t('slashClear', ui.lang) },
  { key: 'compact', label: t('slashCompact', ui.lang) },
])

function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function onInput() {
  autoGrow()
  updateMenu()
}

function updateMenu() {
  const q = draft.value
  if (q.startsWith('/') && !q.includes(' ')) {
    const kw = q.slice(1).toLowerCase()
    menuOpen.value = true
    const match = commands.value.find((c) => c.key === kw)
    activeCmd.value = match ? match.key : commands.value[0]?.key ?? ''
  } else {
    menuOpen.value = false
  }
}

async function onKeydown(e: KeyboardEvent) {
  if (compositionKey(e)) return
  if (e.key === 'Enter' && !e.shiftKey && !composing.value) {
    e.preventDefault()
    if (draft.value.trim()) await submit()
    return
  }
  if (!menuOpen.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    cycle(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cycle(-1)
  } else if (e.key === 'Tab' || e.key === 'Enter') {
    e.preventDefault()
    const cmd = commands.value.find((c) => c.key === activeCmd.value)
    if (cmd) runCommand(cmd)
  } else if (e.key === 'Escape') {
    menuOpen.value = false
  }
}

function compositionKey(e: KeyboardEvent): boolean {
  return e.isComposing || (e.key === 'Enter' && composing.value)
}

function cycle(dir: number) {
  const idx = commands.value.findIndex((c) => c.key === activeCmd.value)
  const next = (idx + dir + commands.value.length) % commands.value.length
  activeCmd.value = commands.value[next]?.key ?? ''
}

async function runCommand(cmd: Command) {
  menuOpen.value = false
  draft.value = ''
  await nextTick(autoGrow)
  if (cmd.key === 'clear') {
    await chat.clearConversation()
  } else if (cmd.key === 'compact') {
    await chat.compactConversation()
  }
}

async function submit() {
  const q = draft.value.trim()
  if (!q || chat.streaming) return
  menuOpen.value = false
  draft.value = ''
  await nextTick(autoGrow)
  await chat.send(q)
}

function onGlobalKey(e: KeyboardEvent) {
  // '/' focuses the composer when not already inside an input
  const target = e.target as HTMLElement
  const tag = target?.tagName
  if (e.key === '/' && tag !== 'TEXTAREA' && tag !== 'INPUT' && tag !== 'SELECT') {
    e.preventDefault()
    inputEl.value?.focus()
  }
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKey)
  updateMenu()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKey)
})
</script>
