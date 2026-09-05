<template>
  <form class="composer" @submit.prevent="submit">
    <div class="composer-box">
      <button
        class="plus-btn"
        type="button"
        :aria-expanded="plusOpen"
        :title="t('attach', ui.lang)"
        @click.stop="plusOpen = !plusOpen"
      >
        <Plus :size="18" :stroke-width="2" />
      </button>

      <textarea
        v-model="draft"
        ref="inputEl"
        class="composer-input"
        rows="1"
        @keydown="onKeydown"
        @compositionstart="composing = true"
        @compositionend="composing = false"
        @input="onInput"
      ></textarea>

      <button
        v-if="!chat.streaming"
        class="send-btn circular"
        type="submit"
        :disabled="!draft.trim()"
      >
        <ArrowUp :size="18" :stroke-width="2.5" />
      </button>
      <button
        v-else
        class="stop-btn circular"
        type="button"
        @click="chat.stop()"
      >
        <Square :size="14" :stroke-width="2.5" />
      </button>

      <div v-if="plusOpen" class="plus-menu" @pointerdown.stop>
        <div class="plus-menu-title">{{ t('datasources', ui.lang) }}</div>
        <button
          v-for="ds in ui.datasourceList"
          :key="ds.name"
          class="plus-item"
          :class="{ active: ds.name === currentDatasource }"
          @click="selectDatasource(ds.name)"
        >
          <Database :size="14" :stroke-width="2" />
          <span class="plus-item-name">{{ ds.name }}</span>
          <span class="plus-item-type">{{ dsTypeLabel(ds.type) }}</span>
          <Check
            v-if="ds.name === currentDatasource"
            class="plus-check"
            :size="14"
          />
        </button>
        <div class="plus-menu-sep"></div>
        <button class="plus-item" @click="pickFile">
          <Upload :size="14" :stroke-width="2" />
          <span>{{ t('uploadFile', ui.lang) }}</span>
          <span v-if="uploading" class="uploading-label">…</span>
        </button>
      </div>
      <input
        ref="fileInput"
        type="file"
        accept=".csv,.tsv,.txt"
        style="display: none"
        @change="onFile"
      />
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
  </form>
</template>

<script setup lang="ts">
import { ref, nextTick, computed, onMounted, onUnmounted } from 'vue'
import {
  Plus,
  Check,
  Database,
  Upload,
  ArrowUp,
  Square,
} from 'lucide-vue-next'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { apiFetch } from '../../api/http'
import { notifyError } from '../../utils/notify'
import { t } from '../../i18n'
import { dsTypeLabel } from '../../utils/format'

const chat = useChatStore()
const ui = useUiStore()
const draft = ref('')
const inputEl = ref<HTMLTextAreaElement>()
const composing = ref(false)

const plusOpen = ref(false)
const fileInput = ref<HTMLInputElement>()
const uploading = ref(false)

const currentDatasource = computed(() => {
  if (ui.datasource) return ui.datasource
  const def = ui.datasourceList.find((ds) => ds.default)
  return def?.name || ui.datasourceList[0]?.name || ''
})

function selectDatasource(name: string) {
  ui.setDatasource(name)
  plusOpen.value = false
}

function pickFile() {
  fileInput.value?.click()
}

async function onFile(e: Event) {
  const el = e.target as HTMLInputElement
  const file = el.files?.[0]
  if (!file) return
  try {
    uploading.value = true
    const form = new FormData()
    form.append('file', file)
    const resp = await apiFetch('/v1/catalog/upload', {
      method: 'POST',
      body: form,
    })
    if (!resp.ok) {
      const err = await resp.text().catch(() => resp.statusText)
      throw new Error(err)
    }
    const body = await resp.json()
    ui.setDatasource(body.datasource)
    await ui.loadDatasources()
  } catch (err) {
    notifyError(String((err as Error)?.message ?? 'upload failed'))
  } finally {
    uploading.value = false
    el.value = ''
  }
}

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
    activeCmd.value = match ? match.key : (commands.value[0]?.key ?? '')
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

/** 外部填入草稿(空态示例问题点按填入,不直接发送)。 */
function fillDraft(q: string) {
  draft.value = q
  void nextTick(autoGrow)
}

defineExpose({ fillDraft })

function onGlobalKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && plusOpen.value) {
    plusOpen.value = false
    return
  }
  // '/' focuses the composer when not already inside an input
  const target = e.target as HTMLElement
  const tag = target?.tagName
  if (
    e.key === '/' &&
    tag !== 'TEXTAREA' &&
    tag !== 'INPUT' &&
    tag !== 'SELECT'
  ) {
    e.preventDefault()
    inputEl.value?.focus()
  }
}

function onGlobalPointer(e: PointerEvent) {
  if (plusOpen.value) {
    const target = e.target as HTMLElement
    if (!target.closest('.composer-box')) plusOpen.value = false
  }
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKey)
  window.addEventListener('pointerdown', onGlobalPointer)
  void ui.loadDatasources()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKey)
  window.removeEventListener('pointerdown', onGlobalPointer)
})
</script>
