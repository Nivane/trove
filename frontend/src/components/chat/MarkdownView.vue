<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown, stripAsciiChart } from '../../utils/markdown'

const props = defineProps<{ source: string }>()

const html = computed(() => renderMarkdown(stripAsciiChart(props.source || '')))
</script>
