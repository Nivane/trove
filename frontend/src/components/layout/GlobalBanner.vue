<!--
  GlobalBanner — 顶栏下沿的全局状态条（设计稿 P6 §2.5）。
  W0 只落最小版：离线（黄）/ 恢复在线（绿）——降级条与会话过期条
  等后端字段/登录页就位后再接（W1/W2），色与语义沿用 tokens.css。
  条带是 role="status" 的礼貌播报，不是打断式 alert。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Wifi, WifiOff, X } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import type { MessageKey } from './navModel'

const props = defineProps<{ kind: 'offline' | 'online' }>()
defineEmits<{ (e: 'dismiss'): void }>()

const ui = useUiStore()

const TEXT_KEYS: Record<'offline' | 'online', MessageKey> = {
  offline: 'netOffline',
  online: 'netOnline',
}
const text = computed(() => t(TEXT_KEYS[props.kind], ui.lang))
</script>

<template>
  <div
    class="global-banner"
    :class="`is-${kind}`"
    role="status"
    aria-live="polite"
  >
    <component
      :is="kind === 'offline' ? WifiOff : Wifi"
      :size="14"
      aria-hidden="true"
    />
    <span class="gb-text">{{ text }}</span>
    <button
      class="gb-close"
      type="button"
      :aria-label="t('close', ui.lang)"
      @click="$emit('dismiss')"
    >
      <X :size="14" aria-hidden="true" />
    </button>
  </div>
</template>

<style scoped>
.global-banner {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  font-size: var(--fs-xs);
  border-bottom: 1px solid var(--border-subtle);
}
.global-banner.is-offline {
  background: var(--warn-bg);
  color: var(--warn-text);
}
.global-banner.is-online {
  background: var(--ok-bg);
  color: var(--ok-text);
}
.gb-text {
  flex: 1;
  min-width: 0;
}
.gb-close {
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border-radius: var(--r-sm);
  color: inherit;
  flex-shrink: 0;
}
.gb-close:hover {
  background: rgba(24, 24, 27, 0.06);
}
</style>
