<template>
  <div class="admin-view">
    <div class="view-header">
      <div>
        <h2>{{ t('kb', ui.lang) }}</h2>
        <p class="view-desc">{{ t('kbPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-header-right">
        <el-button class="refresh-btn" :loading="loading" @click="load">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
      </div>
    </div>

    <div class="admin-tabs">
      <el-tabs v-model="tab">
        <el-tab-pane :name="'pending'">
          <template #label>
            <span class="tab-label">
              {{ t('pendingLessons', ui.lang) }}
              <span v-if="pending.length" class="tab-badge">{{ pending.length }}</span>
            </span>
          </template>
          <div class="admin-card">
            <el-table
              v-loading="loading"
              :data="pending"
              class="admin-table"
              empty-text="—"
            >
              <el-table-column :label="t('kb', ui.lang)" min-width="200">
                <template #default="{ row }">
                  <div class="lesson-cell">
                    <span class="lesson-title">{{ label(row) }}</span>
                    <span v-if="row.pattern && row.question" class="cell-muted">{{
                      '@' + row.pattern
                    }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="note" min-width="220">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.note || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sql" min-width="220">
                <template #default="{ row }">
                  <button
                    v-if="row.sql_snippet"
                    class="sql-snippet"
                    title="copy"
                    @click="copySnippet(row.sql_snippet)"
                  >
                    <code>{{ row.sql_snippet }}</code>
                  </button>
                  <span v-else class="cell-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('upvotes', ui.lang)" width="84">
                <template #default="{ row }">
                  <span class="vote-badge vote-up">
                    <ThumbsUp :size="13" />
                    {{ row.upvotes ?? 0 }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column :label="t('downvotes', ui.lang)" width="84">
                <template #default="{ row }">
                  <span class="vote-badge vote-down">
                    <ThumbsDown :size="13" />
                    {{ row.downvotes ?? 0 }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column :label="t('actions', ui.lang)" width="160" fixed="right">
                <template #default="{ row }">
                  <div class="row-actions">
                    <button
                      class="mini-btn mini-btn-primary"
                      :disabled="acting"
                      @click="confirmLesson(row)"
                    >
                      <Check :size="13" />
                      {{ t('confirmLesson', ui.lang) }}
                    </button>
                    <button
                      class="mini-btn mini-btn-danger"
                      :disabled="acting"
                      @click="rejectLesson(row)"
                    >
                      <X :size="13" />
                      {{ t('rejectLesson', ui.lang) }}
                    </button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="!loading && !pending.length" class="empty-note">
              {{ t('noPending', ui.lang) }}
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane :name="'confirmed'">
          <template #label>
            <span class="tab-label">
              {{ t('confirmedLessons', ui.lang) }}
              <span v-if="confirmed.length" class="tab-badge tab-badge-ok">{{
                confirmed.length
              }}</span>
            </span>
          </template>
          <div class="admin-card">
            <el-table
              v-loading="loading"
              :data="confirmed"
              class="admin-table"
              empty-text="—"
            >
              <el-table-column :label="t('kb', ui.lang)" min-width="200">
                <template #default="{ row }">
                  <div class="lesson-cell">
                    <span class="lesson-title">{{ label(row) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="note" min-width="240">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.note || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sql" min-width="220">
                <template #default="{ row }">
                  <button
                    v-if="row.sql_snippet"
                    class="sql-snippet"
                    title="copy"
                    @click="copySnippet(row.sql_snippet)"
                  >
                    <code>{{ row.sql_snippet }}</code>
                  </button>
                  <span v-else class="cell-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('upvotes', ui.lang)" width="84">
                <template #default="{ row }">
                  <span class="vote-badge vote-up">
                    <ThumbsUp :size="13" />
                    {{ row.upvotes ?? 0 }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column :label="t('downvotes', ui.lang)" width="84">
                <template #default="{ row }">
                  <span class="vote-badge vote-down">
                    <ThumbsDown :size="13" />
                    {{ row.downvotes ?? 0 }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column :label="t('status', ui.lang)" width="110">
                <template #default>
                  <span class="pill pill-ok">
                    <span class="pill-dot pill-dot-ok" />
                    {{ t('confirmed', ui.lang) }}
                  </span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RefreshCw, ThumbsUp, ThumbsDown, Check, X } from 'lucide-vue-next'
import { apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import { copyText } from '../../utils/format'

interface Lesson {
  pattern?: string
  question?: string
  note?: string
  sql_snippet?: string
  confirmed?: boolean
  upvotes?: number
  downvotes?: number
}

function label(l: Lesson): string {
  return l.question || l.pattern || '-'
}

const ui = useUiStore()
const tab = ref('pending')
const pending = ref<Lesson[]>([])
const confirmed = ref<Lesson[]>([])
const loading = ref(false)
const acting = ref(false)

async function load() {
  loading.value = true
  try {
    // pending=true is an unfiltered superset — filter client-side
    const body = await apiGet('/v1/kb/lessons?pending=true')
    const all: Lesson[] = body.lessons ?? []
    pending.value = all.filter((l) => !l.confirmed)
    confirmed.value = all.filter((l) => l.confirmed)
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function copySnippet(sql: string) {
  const ok = await copyText(sql)
  notifySuccess(
    ok ? t('copied', ui.lang) : t('copyFailed', ui.lang),
  )
}

async function confirmLesson(row: Lesson) {
  const key = row.pattern || row.question || ''
  if (!key) return
  acting.value = true
  try {
    await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(key)}/confirm`)
    notifySuccess(t('lessonConfirmedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}

async function rejectLesson(row: Lesson) {
  const key = row.pattern || row.question || ''
  if (!key) return
  acting.value = true
  try {
    await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(key)}/reject`)
    notifySuccess(t('lessonRejectedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}

onMounted(load)
</script>