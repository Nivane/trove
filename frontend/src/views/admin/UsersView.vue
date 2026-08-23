<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('users', ui.lang) }}</h2>
        <p class="view-desc">{{ t('usersPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon accent"><Users :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('users', ui.lang) }}</span>
          <span class="stat-value">{{ users.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon ok"><UserCheck :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('statusActive', ui.lang) }}</span>
          <span class="stat-value">{{ activeCount }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><ShieldCheck :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('adminRole', ui.lang) }}</span>
          <span class="stat-value">{{ adminCount }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon warn"><VolumeX :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('statusDisabled', ui.lang) }}</span>
          <span class="stat-value">{{ disabledCount }}</span>
        </div>
      </div>
    </div>

    <div class="admin-card">
      <div class="card-toolbar">
        <el-input
          v-model="search"
          class="toolbar-search"
          :prefix-icon="Search"
          :placeholder="t('searchUsers', ui.lang)"
          clearable
        />
        <span class="spacer" />
        <span class="view-count">
          {{ filtered.length }} · {{ users.length }}
        </span>
        <el-button type="primary" @click="openCreate">
          <UserPlus :size="15" class="btn-icon" />
          {{ t('createUser', ui.lang) }}
        </el-button>
      </div>

      <div v-if="loading && !filtered.length" class="table-skeleton">
        <div v-for="n in 8" :key="n" class="skeleton-row">
          <el-skeleton :rows="1" animated />
        </div>
      </div>
      <el-table
        v-else
        v-loading="loading"
        :data="filtered"
        class="admin-table"
        max-height="calc(100vh - 320px)"
      >
        <template #empty>
          <TableEmpty>{{ t('emptyFiltered', ui.lang) }}</TableEmpty>
        </template>
        <el-table-column :label="t('username', ui.lang)" min-width="210">
          <template #default="{ row }">
            <div class="user-cell">
              <span
                class="user-avatar"
                :class="{ 'is-admin': row.role === 'admin' }"
                >{{ avatar(row) }}</span>
              <div class="user-meta">
                <span class="user-name">{{ row.username }}</span>
                <span v-if="row.display_name" class="user-sub">{{
                  row.display_name
                }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('role', ui.lang)" width="130">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.role === 'admin' ? 'pill-accent' : 'pill-neutral'"
            >
              {{
                row.role === 'admin'
                  ? t('adminRole', ui.lang)
                  : t('userRole', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('status', ui.lang)" width="110">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.disabled ? 'pill-disabled' : 'pill-ok'"
            >
              <span
                class="pill-dot"
                :class="row.disabled ? 'pill-dot-off' : 'pill-dot-ok'"
              />
              {{
                row.disabled
                  ? t('statusDisabled', ui.lang)
                  : t('statusActive', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('createdAt', ui.lang)" width="160">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('datasources', ui.lang)" min-width="220">
          <template #default="{ row }">
            <el-select
              v-model="row._datasources"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              :max-collapse-tags="2"
              size="default"
              class="grant-select"
              :placeholder="t('grantPlaceholder', ui.lang)"
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
        <el-table-column
          :label="t('actions', ui.lang)"
          width="120"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="row-actions">
              <button
                class="mini-btn icon"
                :title="t('edit', ui.lang)"
                @click="openEdit(row)"
              >
                <Pencil :size="13" />
              </button>
              <button
                class="mini-btn icon"
                :title="t('apiTokens', ui.lang)"
                @click="openTokens(row)"
              >
                <KeyRound :size="13" />
              </button>
              <button
                class="mini-btn icon is-danger"
                :title="t('deleteUser', ui.lang)"
                @click="del(row)"
              >
                <Trash2 :size="13" />
              </button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dlgOpen"
      :title="dlgIsEdit ? t('editUser', ui.lang) : t('createUser', ui.lang)"
      width="440"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item :label="t('username', ui.lang)">
          <el-input
            v-model="dlgForm.username"
            :disabled="dlgIsEdit"
            :placeholder="t('username', ui.lang)"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item :label="t('displayName', ui.lang)">
          <el-input
            v-model="dlgForm.display_name"
            :placeholder="
              dlgIsEdit ? t('displayNameOptional', ui.lang) : undefined
            "
          />
        </el-form-item>
        <el-form-item :label="t('loginPass', ui.lang)">
          <el-input
            v-model="dlgForm.password"
            type="password"
            show-password
            :placeholder="
              dlgIsEdit ? t('passwordKeep', ui.lang) : t('loginPass', ui.lang)
            "
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item :label="t('role', ui.lang)">
          <el-select v-model="dlgForm.role" class="ds-select">
            <el-option :label="t('userRole', ui.lang)" value="user" />
            <el-option :label="t('adminRole', ui.lang)" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="dlgIsEdit" :label="t('status', ui.lang)">
          <el-switch
            v-model="dlgForm.disabled"
            :active-text="t('statusDisabled', ui.lang)"
            :inactive-text="t('statusActive', ui.lang)"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button type="primary" :loading="saving" @click="saveUser">
          {{ t('confirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="tokensOpen"
      :title="`${t('apiTokens', ui.lang)} · ${tokenTarget?.username || ''}`"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <div v-if="tokenRaw" class="token-reveal">
        <div class="token-reveal-title">
          {{ t('tokenRevealTitle', ui.lang) }}
        </div>
        <div class="token-reveal-row">
          <code class="token-reveal-code">{{ tokenRaw }}</code>
          <button
            class="mini-btn icon primary"
            :title="t('copy', ui.lang)"
            @click="copyTokenRaw()"
          >
            <Check v-if="tokenCopied" :size="13" />
            <Copy v-else :size="13" />
          </button>
        </div>
      </div>
      <div class="token-create">
        <el-input
          v-model="tokenForm.label"
          :placeholder="t('tokenLabel', ui.lang)"
          clearable
        />
        <el-input-number
          v-model="tokenForm.ttl"
          :min="0"
          :placeholder="t('tokenTtl', ui.lang)"
        />
        <el-button type="primary" :loading="tokenBusy" @click="createToken">
          <Plus :size="15" class="btn-icon" />
          {{ t('createToken', ui.lang) }}
        </el-button>
      </div>
      <div class="token-hint">{{ t('tokenTtlHint', ui.lang) }}</div>
      <div class="token-list">
        <div v-if="!tokens.length && !tokenBusy" class="empty-note">
          {{ t('noTokens', ui.lang) }}
        </div>
        <div v-for="tk in tokens" :key="tk.id" class="token-row">
          <span
            class="token-dot"
            :class="tk.revoked ? 'token-dot-off' : 'token-dot-ok'"
          />
          <div class="token-meta">
            <span class="token-label">{{ tk.label || '—' }}</span>
            <span class="token-sub">
              {{ tk.revoked ? t('revoked', ui.lang) : t('created', ui.lang) }} ·
              {{ fmtDateTime(tk.created_at) }}
              <template v-if="tk.expires_at">
                · {{ t('expiresAt', ui.lang) }} {{ fmtDateTime(tk.expires_at) }}
              </template>
            </span>
          </div>
          <button
            v-if="!tk.revoked"
            class="mini-btn icon is-danger"
            :title="t('revoke', ui.lang)"
            @click="revokeToken(tk)"
          >
            <X :size="13" />
          </button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  UserPlus,
  Plus,
  Check,
  X,
  Copy,
  Users,
  UserCheck,
  ShieldCheck,
  VolumeX,
  KeyRound,
  Trash2,
  Search,
} from 'lucide-vue-next'
import { apiGet, apiPost, apiPatch, apiDelete, apiPut } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import { fmtDateTime, copyText } from '../../utils/format'
import TableEmpty from '../../components/admin/TableEmpty.vue'

interface TokenRow {
  id: number
  label?: string
  revoked?: number | boolean
  created_at?: string
  expires_at?: string | null
}

interface UserRow {
  id: number
  username: string
  display_name: string
  role: string
  disabled: boolean
  created_at?: string
  _datasources: string[]
}

const ui = useUiStore()
const users = ref<UserRow[]>([])
const loading = ref(false)
const saving = ref(false)
const search = ref('')
const knownDatasources = ref<string[]>([])

const activeCount = computed(
  () => users.value.filter((u) => !u.disabled).length,
)
const adminCount = computed(
  () => users.value.filter((u) => u.role === 'admin').length,
)
const disabledCount = computed(() => users.value.length - activeCount.value)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) =>
      u.username.toLowerCase().includes(q) ||
      (u.display_name || '').toLowerCase().includes(q),
  )
})

const dlgOpen = ref(false)
const dlgIsEdit = ref(false)
const editingId = ref<number | null>(null)
const dlgForm = reactive({
  username: '',
  password: '',
  display_name: '',
  role: 'user',
  disabled: false,
})

// ── API tokens ──
const tokensOpen = ref(false)
const tokenTarget = ref<UserRow | null>(null)
const tokens = ref<TokenRow[]>([])
const tokenBusy = ref(false)
const tokenRaw = ref('')
const tokenCopied = ref(false)
const tokenForm = reactive({
  label: '',
  ttl: 0,
})

function avatar(row: UserRow): string {
  return (row.display_name || row.username || '?').trim()[0].toUpperCase()
}

async function load() {
  loading.value = true
  try {
    const [uBody, dsBody] = await Promise.all([
      apiGet('/v1/admin/users'),
      apiGet('/v1/catalog/datasources'),
    ])
    knownDatasources.value = (dsBody.datasources ?? []).map(
      (d: { name: string }) => d.name,
    )
    const list = (uBody.users ?? []) as UserRow[]
    // hydrate grants in parallel (no N+1 serial round-trips)
    const grants = await Promise.all(
      list.map((u) =>
        apiGet(`/v1/admin/users/${u.id}/datasources`)
          .then((g) => g.datasources ?? [])
          .catch(() => [] as string[]),
      ),
    )
    users.value = list.map((u, i) => ({ ...u, _datasources: grants[i] ?? [] }))
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  dlgIsEdit.value = false
  editingId.value = null
  Object.assign(dlgForm, {
    username: '',
    password: '',
    display_name: '',
    role: 'user',
    disabled: false,
  })
  dlgOpen.value = true
}

function openEdit(row: UserRow) {
  dlgIsEdit.value = true
  editingId.value = row.id
  Object.assign(dlgForm, {
    username: row.username,
    password: '',
    display_name: row.display_name ?? '',
    role: row.role,
    disabled: !!row.disabled,
  })
  dlgOpen.value = true
}

async function saveUser() {
  if (!dlgForm.username.trim()) {
    toastError(
      new Error(t('errUserRequired', ui.lang)),
      t('errUserRequired', ui.lang),
    )
    return
  }
  if (!dlgIsEdit.value && !dlgForm.password) {
    toastError(
      new Error(t('errPassRequired', ui.lang)),
      t('errPassRequired', ui.lang),
    )
    return
  }
  saving.value = true
  try {
    if (dlgIsEdit.value && editingId.value != null) {
      const body: Record<string, unknown> = {
        role: dlgForm.role,
        display_name: dlgForm.display_name,
        disabled: dlgForm.disabled,
      }
      if (dlgForm.password) body.password = dlgForm.password
      await apiPatch(`/v1/admin/users/${editingId.value}`, body)
      notifySuccess(t('userUpdatedOk', ui.lang))
    } else {
      await apiPost('/v1/admin/users', {
        username: dlgForm.username,
        password: dlgForm.password,
        display_name: dlgForm.display_name,
        role: dlgForm.role,
      })
      notifySuccess(t('userCreatedOk', ui.lang))
    }
    dlgOpen.value = false
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function saveDatasources(row: UserRow, v: string[]) {
  try {
    await apiPut(`/v1/admin/users/${row.id}/datasources`, { datasources: v })
    notifySuccess(t('grantsUpdatedOk', ui.lang))
  } catch (e) {
    toastError(e)
    await load()
  }
}

async function del(row: UserRow) {
  try {
    await ElMessageBox.confirm(
      `${t('confirmDelete', ui.lang)} (${row.username})`,
      t('deleteUser', ui.lang),
      { type: 'warning', confirmButtonText: t('delete', ui.lang) },
    )
  } catch {
    return
  }
  try {
    await apiDelete(`/v1/admin/users/${row.id}`)
    notifySuccess(t('userDeletedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function openTokens(row: UserRow) {
  tokenTarget.value = row
  tokens.value = []
  tokenRaw.value = ''
  Object.assign(tokenForm, { label: '', ttl: 0 })
  tokensOpen.value = true
  await loadTokens()
}

async function loadTokens() {
  if (tokenTarget.value == null) return
  tokenBusy.value = true
  try {
    const body = await apiGet(`/v1/admin/users/${tokenTarget.value.id}/tokens`)
    tokens.value = (body.tokens ?? []) as TokenRow[]
  } catch (e) {
    toastError(e)
  } finally {
    tokenBusy.value = false
  }
}

async function createToken() {
  if (tokenTarget.value == null) return
  tokenBusy.value = true
  tokenRaw.value = ''
  try {
    const body = await apiPost(
      `/v1/admin/users/${tokenTarget.value.id}/tokens`,
      {
        label: tokenForm.label,
        ttl_hours: tokenForm.ttl > 0 ? tokenForm.ttl : undefined,
      },
    )
    tokenRaw.value = body.token as string
    tokenForm.label = ''
    tokenForm.ttl = 0
    notifySuccess(t('tokenCreatedOk', ui.lang))
    await loadTokens()
  } catch (e) {
    toastError(e)
  } finally {
    tokenBusy.value = false
  }
}

async function revokeToken(tk: TokenRow) {
  try {
    await apiDelete(`/v1/admin/tokens/${tk.id}`)
    notifySuccess(t('tokenRevokedOk', ui.lang))
    await loadTokens()
  } catch (e) {
    toastError(e)
  }
}

async function copyTokenRaw() {
  if (!tokenRaw.value) return
  const ok = await copyText(tokenRaw.value)
  if (!ok) {
    notifySuccess(t('copyFailed', ui.lang))
    return
  }
  tokenCopied.value = true
  window.setTimeout(() => (tokenCopied.value = false), 1600)
}

onMounted(load)
</script>
