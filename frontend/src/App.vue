<template>
  <router-view />
</template>

<script setup lang="ts">
import { useUiStore } from './stores/ui'
import { useAuthStore } from './stores/auth'

const ui = useUiStore()
const auth = useAuthStore()
ui.applyTheme()
void auth.bootstrap()
</script>
