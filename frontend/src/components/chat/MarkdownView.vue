<template>
  <div class="markdown-blocks">
    <template v-for="(b, idx) in blocks" :key="idx">
      <div
        v-if="b.type === 'md'"
        class="markdown-body"
        v-html="renderMd(b.text)"
      />
      <DataTable
        v-else-if="b.type === 'table'"
        :headers="b.headers"
        :rows="b.rows"
        :download-rows="resultRows"
      />
      <SqlBlock v-else-if="b.type === 'sql'" :code="b.code" />
      <details v-else-if="b.type === 'details'" class="answer-details">
        <summary class="answer-details-summary">
          <span class="answer-details-caret" aria-hidden="true" />
          {{ b.summary }}
        </summary>
        <div class="answer-details-body">
          <template v-for="(inner, j) in b.blocks" :key="j">
            <div
              v-if="inner.type === 'md'"
              class="markdown-body"
              v-html="renderMd(inner.text)"
            />
            <DataTable
              v-else-if="inner.type === 'table'"
              :headers="inner.headers"
              :rows="inner.rows"
              :download-rows="resultRows"
            />
            <SqlBlock v-else-if="inner.type === 'sql'" :code="inner.code" />
          </template>
        </div>
      </details>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DataTable from './DataTable.vue'
import SqlBlock from './SqlBlock.vue'
import { tokenize } from '../../utils/blocks'
import { renderMarkdown, stripAsciiChart } from '../../utils/markdown'

const props = defineProps<{
  source: string
  /** 完整查询结果(可选,供结果表格"按查询结果下载")。 */
  resultRows?: unknown[][] | null
}>()

const cleaned = computed(() => stripAsciiChart(props.source || ''))
const blocks = computed(() => tokenize(cleaned.value))

function renderMd(text: string): string {
  return renderMarkdown(text)
}
</script>
