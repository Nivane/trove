<template>
  <div class="welcome">
    <div class="welcome-mark">◆</div>
    <h1 class="welcome-title">{{ t('welcomeTitle', ui.lang) }}</h1>
    <p class="welcome-subtitle">{{ t('welcomeSubtitle', ui.lang) }}</p>
  </div>
</template>

<script setup lang="ts">
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const ui = useUiStore()
</script>
