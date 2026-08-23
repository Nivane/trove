<template>
  <div class="table-empty">
    <Inbox :size="20" class="table-empty-icon" />
    <span class="table-empty-text"><slot>{{ t('emptyData', ui.lang) }}</slot></span>
  </div>
</template>

<script setup lang="ts">
import { Inbox } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const ui = useUiStore()
</script>

<style scoped>
.table-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-8) var(--sp-4);
  color: var(--text-tertiary);
}
.table-empty-icon {
  opacity: 0.55;
}
.table-empty-text {
  font-size: var(--fs-sm);
}
</style>
