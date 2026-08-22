<template>
  <div class="admin-view">
    <div class="view-header">
      <h2>{{ t('users', ui.lang) }}</h2>
      <el-button type="primary" @click="openCreate">{{
        t('createUser', ui.lang)
      }}</el-button>
    </div>

    <el-table :data="users" v-loading="loading" class="users-table">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column
        prop="username"
        :label="t('username', ui.lang)"
        min-width="140"
      />
      <el-table-column
        prop="display_name"
        :label="t('displayName', ui.lang)"
        min-width="140"
      />
      <el-table-column prop="role" :label="t('role', ui.lang)" width="90" />
      <el-table-column :label="t('disabled', ui.lang)" width="90">
        <template #default="{ row }">
          <el-tag :type="row.disabled ? 'danger' : 'success'">{{
            row.disabled ? '✓' : '–'
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('datasources', ui.lang)" min-width="160">
        <template #default="{ row }">
          <el-select
            v-model="row._datasources"
            multiple
            filterable
            allow-create
            size="small"
            placeholder="—"
            @change="(v: string[]) => saveDatasources(row, v)"
          >
            <el-option
              v-for="d in knownDatasources"
              :key="d"
              :label="d"
              :value="d"
            />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('actions', ui.lang)" width="300" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="resetPassword(row)">{{
            t('resetPassword', ui.lang)
          }}</el-button>
          <el-button size="small" @click="toggleDisabled(row)">
            {{ row.disabled ? t('enable', ui.lang) : t('disable', ui.lang) }}
          </el-button>
          <el-button size="small" type="danger" @click="del(row)">{{
            t('deleteUser', ui.lang)
          }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="createOpen"
      :title="t('createUser', ui.lang)"
      width="420"
    >
      <el-form label-width="90">
        <el-form-item :label="t('username', ui.lang)">
          <el-input v-model="createForm.username" />
        </el-form-item>
        <el-form-item :label="t('loginPass', ui.lang)">
          <el-input v-model="createForm.password" show-password />
        </el-form-item>
        <el-form-item :label="t('displayName', ui.lang)">
          <el-input v-model="createForm.display_name" />
        </el-form-item>
        <el-form-item :label="t('role', ui.lang)">
          <el-switch
            v-model="createForm.isAdmin"
            active-text="admin"
            inactive-text="user"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">Cancel</el-button>
        <el-button type="primary" @click="create">OK</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiGet, apiPost, apiPatch, apiDelete, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

interface UserRow {
  id: number
  username: string
  display_name: string
  role: string
  disabled: boolean
  _datasources: string[]
}

const ui = useUiStore()
const users = ref<UserRow[]>([])
const loading = ref(false)
const createOpen = ref(false)
const createForm = reactive({
  username: '',
  password: '',
  display_name: '',
  isAdmin: false,
})
const knownDatasources = ref<string[]>([])

async function load() {
  loading.value = true
  try {
    const body = await apiGet('/v1/admin/users')
    const ds = await apiGet('/v1/catalog/datasources')
    knownDatasources.value = (ds.datasources ?? []).map(
      (d: { name: string }) => d.name,
    )
    users.value = []
    for (const u of body.users ?? []) {
      const grants = await apiGet(`/v1/admin/users/${u.id}/datasources`)
      users.value.push({ ...u, _datasources: grants.datasources ?? [] })
    }
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(createForm, {
    username: '',
    password: '',
    display_name: '',
    isAdmin: false,
  })
  createOpen.value = true
}

async function create() {
  try {
    await apiPost('/v1/admin/users', {
      username: createForm.username,
      password: createForm.password,
      display_name: createForm.display_name,
      role: createForm.isAdmin ? 'admin' : 'user',
    })
    createOpen.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(String(e.message ?? e))
  }
}

async function saveDatasources(row: UserRow, v: string[]) {
  await apiPut(`/v1/admin/users/${row.id}/datasources`, { datasources: v })
}

async function resetPassword(row: UserRow) {
  try {
    const { value } = await ElMessageBox.prompt(
      'New password',
      'Reset password',
    )
    await apiPatch(`/v1/admin/users/${row.id}`, { password: value })
    ElMessage.success('ok')
  } catch {
    /* cancelled */
  }
}

async function toggleDisabled(row: UserRow) {
  try {
    await apiPatch(`/v1/admin/users/${row.id}`, { disabled: !row.disabled })
    await load()
  } catch (e: any) {
    ElMessage.error(String(e.message ?? e))
  }
}

async function del(row: UserRow) {
  try {
    await ElMessageBox.confirm(t('confirmDelete', ui.lang), 'Confirm')
  } catch {
    return
  }
  await apiDelete(`/v1/admin/users/${row.id}`)
  await load()
}

onMounted(load)
</script>
