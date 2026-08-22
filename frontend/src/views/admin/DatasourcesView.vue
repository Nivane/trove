<template>
  <div class="admin-view">
    <div class="view-header">
      <div>
        <h2>{{ t('datasources', ui.lang) }}</h2>
        <p class="view-desc">{{ t('dsPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-header-meta">
        <span class="view-count">{{ rows.length }} · {{ t('datasources', ui.lang) }}</span>
      </div>
    </div>

    <div class="ds-create">
      <div class="ds-create-head">
        <span class="ds-create-title">{{ t('dsCreateTitle', ui.lang) }}</span>
      </div>
      <div class="ds-create-row">
        <el-input
          v-model="newName"
          :placeholder="t('dsName', ui.lang)"
          class="ds-name-input"
          clearable
          @keyup.enter="add"
        />
        <el-input
          v-model="newUrl"
          :placeholder="t('dsUrl', ui.lang)"
          class="ds-url-input"
          clearable
          @keyup.enter="add"
        />
        <el-button
          type="primary"
          class="add"
          :disabled="!newUrl.trim() && !newName.trim()"
          :loading="adding"
          @click="add"
        >
          <Plus :size="15" class="btn-icon" />
          {{ t('dsAdd', ui.lang) }}
        </el-button>
      </div>
      <div class="ds-create-hint">
        {{ t('dsAddHint', ui.lang) }} · {{ t('dsUrlExample', ui.lang) }}
      </div>
    </div>

    <div class="admin-card">
      <el-table
        v-loading="loading"
        :data="rows"
        class="admin-table"
        empty-text="—"
      >
        <el-table-column :label="t('datasources', ui.lang)" min-width="200">
          <template #default="{ row }">
            <div class="ds-name-cell">
              <span class="ds-icon"><Database :size="15" /></span>
              <div class="ds-name-meta">
                <span class="ds-name">{{ row.name }}</span>
                <span class="ds-type">{{ row.type }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('dsStatus', ui.lang)" width="130">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.status === 'connected' ? 'pill-ok' : 'pill-neutral'"
            >
              <span
                class="pill-dot"
                :class="row.status === 'connected' ? 'pill-dot-ok' : ''"
              />
              {{
                row.status === 'connected'
                  ? t('dsConnected', ui.lang)
                  : t('dsDisconnected', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('dsKb', ui.lang)" width="150">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.kb_initialized ? 'pill-ok' : 'pill-warn'"
            >
              {{
                row.kb_initialized
                  ? t('dsKbReady', ui.lang)
                  : t('dsKbNotReady', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="default" width="92">
          <template #default="{ row }">
            <span v-if="row.default" class="pill pill-accent">default</span>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('actions', ui.lang)"
          width="270"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="row-actions">
              <button
                v-if="!row.kb_initialized"
                class="mini-btn init"
                :disabled="busy(row.name, 'init')"
                @click="initKb(row)"
              >
                {{ t('dsInit', ui.lang) }}
              </button>
              <button
                v-if="row.kb_initialized"
                class="mini-btn"
                :disabled="busy(row.name, 'reload')"
                @click="reloadKb(row)"
              >
                {{ t('dsReload', ui.lang) }}
              </button>
              <button
                class="mini-btn"
                :disabled="busy(row.name, 'reconnect')"
                @click="reconnect(row)"
              >
                {{ t('dsReconnect', ui.lang) }}
              </button>
              <button class="mini-btn is-danger" @click="remove(row)">
                {{ t('dsRemove', ui.lang) }}
              </button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  Plus,
  Database,
} from 'lucide-vue-next'
import { apiDelete, apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import type { DatasourceInfo } from '../../api/types'

const ui = useUiStore()
const rows = ref<DatasourceInfo[]>([])
const loading = ref(false)
const adding = ref(false)
const newName = ref('')
const newUrl = ref('')
const busyMap = reactive<Record<string, boolean>>({})

function busyKey(row: DatasourceInfo, action: string) {
  return `${row.name}:${action}`
}
function busy(row: DatasourceInfo, action: string): boolean {
  return !!busyMap[busyKey(row, action)]
}
function setBusy(row: DatasourceInfo, action: string, v: boolean) {
  if (v) busyMap[busyKey(row, action)] = true
  else delete busyMap[busyKey(row, action)]
}

async function load() {
  loading.value = true
  try {
    rows.value = (await apiGet('/v1/admin/datasources')).datasources ?? []
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function add() {
  const name = newName.value.trim()
  const url = newUrl.value.trim()
  if (!name && !url) return
  adding.value = true
  try {
    await apiPost('/v1/admin/datasources', { name, url })
    newName.value = ''
    newUrl.value = ''
    notifySuccess(t('dsAddedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e, t('dsAddFail', ui.lang))
  } finally {
    adding.value = false
  }
}

async function initKb(row: DatasourceInfo) {
  try {
    await ElMessageBox.confirm(t('dsInitConfirm', ui.lang), 'Confirm')
  } catch {
    return
  }
  setBusy(row, 'init', true)
  try {
    await apiPost(`/v1/admin/datasources/${row.name}/kb/init`, {})
    notifySuccess(t('dsInitDone', ui.lang))
    await load()
  } catch (e) {
    toastError(e, t('dsInitFail', ui.lang))
  } finally {
    setBusy(row, 'init', false)
  }
}

async function runAction(
  row: DatasourceInfo,
  action: 'reload' | 'reconnect',
  pathSuffix: string,
  okKey: 'dsReloadDone' | 'dsReconnectDone',
) {
  setBusy(row, action, true)
  try {
    await apiPost(`/v1/admin/datasources/${row.name}${pathSuffix}`)
    notifySuccess(t(okKey, ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, action, false)
  }
}

async function reloadKb(row: DatasourceInfo) {
  await runAction(row, 'reload', '/kb/reload', 'dsReloadDone')
}

async function reconnect(row: DatasourceInfo) {
  await runAction(row, 'reconnect', '/reconnect', 'dsReconnectDone')
}

async function remove(row: DatasourceInfo) {
  try {
    await ElMessageBox.confirm(t('dsRemoveConfirm', ui.lang), 'Confirm')
  } catch {
    return
  }
  setBusy(row, 'remove', true)
  try {
    await apiDelete(`/v1/admin/datasources/${row.name}`)
    notifySuccess(t('dsRemovedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, 'remove', false)
  }
}

onMounted(load)
</script>