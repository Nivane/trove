<template>
  <div class="hitl-card">
    <div class="hitl-hint">{{ t('hitlHint', ui.lang) }}</div>
    <div v-if="!chosen" class="hitl-actions">
      <button class="hitl-btn approve" @click="choose('yes')">{{ t('hitlApprove', ui.lang) }}</button>
      <button v-if="batch" class="hitl-btn approve-all" @click="choose('approve_all')">
        {{ t('hitlApproveAll', ui.lang) }}
      </button>
      <button class="hitl-btn reject" @click="choose('no')">{{ t('hitlReject', ui.lang) }}</button>
    </div>
    <div v-else class="hitl-chosen">
      <span class="badge">{{ chosenLabel }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const props = defineProps<{ batch: boolean }>()
const chat = useChatStore()
const ui = useUiStore()

// click-to-replace: after the first click the buttons are replaced by a
// badge (vanilla behavior port) — Vue binding + sent guard make the old
// onclick-override hack unnecessary.
const chosen = ref(false)
const chosenLabel = ref('')

async function choose(decision: 'yes' | 'approve_all' | 'no') {
  if (chosen.value) return
  chosen.value = true
  chosenLabel.value =
    decision === 'approve_all' ? t('hitlApproveAll', ui.lang)
    : decision === 'yes' ? t('hitlApprove', ui.lang)
    : t('hitlReject', ui.lang)
  await chat.resume(decision)
}
</script>
