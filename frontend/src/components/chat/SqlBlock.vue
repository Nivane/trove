<template>
  <div class="sql-block" :class="{ wrap: wrap }">
    <div class="sql-block-bar">
      <span class="sql-block-label">SQL</span>
      <span class="sql-block-actions">
        <button class="icon-btn" :title="t('copySql', ui.lang)" @click="copy">
          <el-icon :size="13"><CopyDocument /></el-icon>
        </button>
      </span>
    </div>
    <pre class="sql-block-pre"><code class="hljs" v-html="highlighted"></code></pre>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import highlight from 'highlight.js/lib/core'
import sql from 'highlight.js/lib/languages/sql'
import { CopyDocument } from '@element-plus/icons-vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, notifyError } from '../../utils/notify'

highlight.registerLanguage('sql', sql)

const props = withDefaults(defineProps<{ code: string; wrap?: boolean }>(), { wrap: true })
const ui = useUiStore()

const highlighted = computed(() => {
  try {
    return highlight.highlight(props.code, { language: 'sql', ignoreIllegals: true }).value
  } catch {
    return escapeHtml(props.code)
  }
})

async function copy() {
  try {
    await navigator.clipboard.writeText(props.code)
    notifySuccess(t('copied', ui.lang))
  } catch {
    notifyError(t('copyFailed', ui.lang))
  }
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
</script>
