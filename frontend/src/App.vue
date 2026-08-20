<template>
  <router-view />
</template>

<script setup lang="ts">
import { useUiStore } from './stores/ui'

const ui = useUiStore()
ui.applyTheme()
</script>
