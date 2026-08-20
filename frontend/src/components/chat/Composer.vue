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
        @input="autoGrow"
      ></textarea>
      <button
        v-if="!chat.streaming"
        class="send-btn"
        type="submit"
        :disabled="!draft.trim()"
      >{{ t('send', ui.lang) }}</button>
      <button v-else class="stop-btn" type="button" @click="chat.stop()">{{ t('stop', ui.lang) }}</button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const chat = useChatStore()
const ui = useUiStore()
const draft = ref('')
const inputEl = ref<HTMLTextAreaElement>()
const composing = ref(false)

function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

async function onKeydown(e: KeyboardEvent) {
  // IME composition must not trigger Enter submit (vanilla behavior port)
  if (e.key === 'Enter' && !e.shiftKey && !composing.value) {
    e.preventDefault()
    if (draft.value.trim()) await submit()
  }
}

async function submit() {
  const q = draft.value.trim()
  if (!q || chat.streaming) return
  draft.value = ''
  await nextTick(autoGrow)
  await chat.send(q)
}
</script>
