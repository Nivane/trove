<template>
  <div class="datasources-view">
    <div class="ds-toolbar">
      <el-input v-model="newName" :placeholder="t('dsName', ui.lang)" style="width: 180px" />
      <el-input v-model="newUrl" :placeholder="t('dsUrl', ui.lang)" style="width: 320px" />
      <el-button type="primary" class="add" :loading="busy" @click="add">{{ t('dsAdd', ui.lang) }}</el-button>
    </div>
    <el-table :data="rows" v-loading="loading" class="ds-table">
      <el-table-column prop="name" :label="t('datasources', ui.lang)" width="160" />
      <el-table-column prop="type" label="Type" width="100" />
      <el-table-column :label="t('dsStatus', ui.lang)" width="120">
        <template #default="{ row }">
          <el-tag :type="row.status === 'connected' ? 'success' : 'danger'">
            {{ row.status === 'connected' ? t('dsConnected', ui.lang) : t('dsDisconnected', ui.lang) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('dsKb', ui.lang)" min-width="160">
        <template #default="{ row }">
          <el-tag v-if="row.kb_initialized" type="info">{{ t('dsKbReady', ui.lang) }}</el-tag>
          <el-tag v-else type="warning">{{ t('dsKbNotReady', ui.lang) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('actions', ui.lang)" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" class="init" :loading="busy" @click="initKb(row)">{{ t('dsInit', ui.lang) }}</el-button>
          <el-button size="small" @click="reloadKb(row)">{{ t('dsReload', ui.lang) }}</el-button>
          <el-button size="small" @click="reconnect(row)">{{ t('dsReconnect', ui.lang) }}</el-button>
          <el-button size="small" type="danger" @click="remove(row)">{{ t('dsRemove', ui.lang) }}</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiDelete, apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'  // 模块函数 t(key, lang)——不是 ui store 方法（UsersView 同款用法）
import type { DatasourceInfo } from '../../api/types'

const ui = useUiStore()
const rows = ref<DatasourceInfo[]>([])
const loading = ref(false)
const busy = ref(false)
const newName = ref('')
const newUrl = ref('')

async function load() {
  loading.value = true
  try {
    rows.value = (await apiGet('/v1/admin/datasources')).datasources ?? []
  } finally {
    loading.value = false
  }
}
async function add() {
  busy.value = true
  try {
    await apiPost('/v1/admin/datasources', { name: newName.value, url: newUrl.value })
    newName.value = ''
    newUrl.value = ''
    await load()
  } finally {
    busy.value = false
  }
}
async function initKb(row: DatasourceInfo) {
  busy.value = true
  try {
    await apiPost(`/v1/admin/datasources/${row.name}/kb/init`, {})
    await load()
  } finally {
    busy.value = false
  }
}
async function reloadKb(row: DatasourceInfo) {
  busy.value = true
  try { await apiPost(`/v1/admin/datasources/${row.name}/kb/reload`); await load() } finally { busy.value = false }
}
async function reconnect(row: DatasourceInfo) {
  busy.value = true
  try { await apiPost(`/v1/admin/datasources/${row.name}/reconnect`); await load() } finally { busy.value = false }
}
async function remove(row: DatasourceInfo) {
  busy.value = true
  try { await apiDelete(`/v1/admin/datasources/${row.name}`); await load() } finally { busy.value = false }
}
onMounted(load)
</script>
