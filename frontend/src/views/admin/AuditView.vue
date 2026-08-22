<template>
  <div class="admin-view">
    <div class="view-header">
      <h2>{{ t('audit', ui.lang) }}</h2>
      <div class="audit-filters">
        <el-input
          v-model="action"
          :placeholder="t('auditAction', ui.lang)"
          clearable
          style="width: 200px"
          @keyup.enter="load"
        />
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>
    <el-table :data="entries" v-loading="loading" class="audit-table">
      <el-table-column prop="ts" :label="t('auditTime', ui.lang)" width="210" />
      <el-table-column
        prop="username"
        :label="t('auditUser', ui.lang)"
        width="130"
      />
      <el-table-column
        prop="action"
        :label="t('auditAction', ui.lang)"
        min-width="160"
      />
      <el-table-column prop="method" label="method" width="80" />
      <el-table-column
        prop="path"
        :label="t('auditPath', ui.lang)"
        min-width="200"
      />
      <el-table-column
        prop="status"
        :label="t('auditStatus', ui.lang)"
        width="80"
      />
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { apiGet } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const ui = useUiStore()
const entries = ref<Record<string, unknown>[]>([])
const loading = ref(false)
const action = ref('')

async function load() {
  loading.value = true
  try {
    const q = action.value ? `?action=${encodeURIComponent(action.value)}` : ''
    const body = await apiGet(`/v1/admin/audit${q}`)
    entries.value = body.audit ?? []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
