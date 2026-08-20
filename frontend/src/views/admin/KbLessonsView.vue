<template>
  <div class="admin-view">
    <div class="view-header">
      <h2>{{ t('kb', ui.lang) }}</h2>
      <el-button size="small" @click="load">{{ t('brand', ui.lang) }}</el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane :label="t('pendingLessons', ui.lang)" name="pending">
        <el-table :data="pending" v-loading="loading" empty-text="—">
          <el-table-column prop="pattern" label="pattern" min-width="220" />
          <el-table-column prop="note" label="note" min-width="260" />
          <el-table-column prop="sql_snippet" label="sql" min-width="180" />
          <el-table-column :label="t('actions', ui.lang)" width="160" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="primary" @click="confirmLesson(row)">
                {{ t('confirmLesson', ui.lang) }}
              </el-button>
              <el-button size="small" type="danger" @click="rejectLesson(row)">
                {{ t('rejectLesson', ui.lang) }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!loading && !pending.length" class="empty-note">{{ t('noPending', ui.lang) }}</div>
      </el-tab-pane>
      <el-tab-pane :label="t('confirmedLessons', ui.lang)" name="confirmed">
        <el-table :data="confirmed" v-loading="loading">
          <el-table-column prop="pattern" label="pattern" min-width="220" />
          <el-table-column prop="note" label="note" min-width="260" />
          <el-table-column prop="sql_snippet" label="sql" min-width="180" />
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

interface Lesson {
  pattern: string
  note?: string
  sql_snippet?: string
  confirmed?: boolean
}

const ui = useUiStore()
const tab = ref('pending')
const pending = ref<Lesson[]>([])
const confirmed = ref<Lesson[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    // pending=true is an unfiltered superset — filter client-side
    const body = await apiGet('/v1/kb/lessons?pending=true')
    const all: Lesson[] = body.lessons ?? []
    pending.value = all.filter((l) => !l.confirmed)
    confirmed.value = all.filter((l) => l.confirmed)
  } finally {
    loading.value = false
  }
}

async function confirmLesson(row: Lesson) {
  await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(row.pattern)}/confirm`)
  ElMessage.success('confirmed')
  await load()
}

async function rejectLesson(row: Lesson) {
  await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(row.pattern)}/reject`)
  ElMessage.success('rejected')
  await load()
}

onMounted(load)
</script>
