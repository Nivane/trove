<template>
  <div class="markdown-blocks">
    <template v-for="(b, idx) in blocks" :key="idx">
      <div v-if="b.type === 'md'" class="markdown-body" v-html="renderMd(b.text)"></div>
      <DataTable v-else-if="b.type === 'table'" :headers="b.headers" :rows="b.rows" />
      <SqlBlock v-else-if="b.type === 'sql'" :code="b.code" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DataTable from './DataTable.vue'
import SqlBlock from './SqlBlock.vue'
import { tokenize } from '../../utils/blocks'
import { renderMarkdown, stripAsciiChart } from '../../utils/markdown'

const props = defineProps<{ source: string }>()

const cleaned = computed(() => stripAsciiChart(props.source || ''))
const blocks = computed(() => tokenize(cleaned.value))

function renderMd(text: string): string {
  return renderMarkdown(text)
}
</script>
