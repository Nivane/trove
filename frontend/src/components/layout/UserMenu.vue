<!--
  UserMenu — 账号菜单的唯一实现（设计稿 P6 §2.1「账号区」/ D5）。
  展开态与折叠态共用这一份（原先 AdminLayout 里是两份拷贝）。
  · 角色徽：admin / analyst / user（后端 USER_ROLES 三值，D9）
  · 语言：局部响应式切换，不整页 reload（D6；html lang 由 ui store 同步）
  · 登出：走 ConfirmDialog 确认，不再一键即出（D7）
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Languages, LogOut, MessageSquare } from 'lucide-vue-next'
import { useAuthStore, type Role } from '../../stores/auth'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import ConfirmDialog from '../base/ConfirmDialog.vue'

const props = defineProps<{
  /** 折叠（rail）态：只留头像，名字进 aria-label。 */
  rail?: boolean
}>()

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()

const displayName = computed(
  () => auth.user?.display_name || auth.user?.username || '',
)
const avatarChar = computed(() =>
  (displayName.value.trim()[0] || '?').toUpperCase(),
)

const ROLE_LABEL_KEYS: Record<Role, 'adminRole' | 'analystRole' | 'userRole'> =
  {
    admin: 'adminRole',
    analyst: 'analystRole',
    user: 'userRole',
  }
const roleLabel = computed(() =>
  auth.user ? t(ROLE_LABEL_KEYS[auth.user.role], ui.lang) : '',
)

const confirmOpen = ref(false)
const logouting = ref(false)

function onCmd(cmd: string | number | object) {
  if (cmd === 'lang') {
    ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
  } else if (cmd === 'chat') {
    void router.push('/')
  } else if (cmd === 'logout') {
    confirmOpen.value = true
  }
}

async function doLogout() {
  logouting.value = true
  try {
    await auth.logout()
    await router.push({ name: 'login' })
  } finally {
    logouting.value = false
    confirmOpen.value = false
  }
}
</script>

<template>
  <el-dropdown trigger="click" popper-class="profile-popper" @command="onCmd">
    <button
      class="profile-btn"
      :class="{ 'profile-rail-btn': props.rail }"
      :aria-label="props.rail ? displayName : undefined"
      :title="props.rail ? displayName : undefined"
    >
      <span class="profile-avatar" aria-hidden="true">{{ avatarChar }}</span>
      <span v-if="!props.rail" class="profile-name">{{ displayName }}</span>
    </button>
    <template #dropdown>
      <div class="profile-head">
        <span class="profile-head-avatar" aria-hidden="true">{{
          avatarChar
        }}</span>
        <div class="profile-head-meta">
          <div class="profile-head-name">{{ displayName }}</div>
          <div class="profile-head-sub">
            {{ auth.user?.username || '' }}
            <span v-if="roleLabel" class="profile-role">{{ roleLabel }}</span>
          </div>
        </div>
      </div>
      <el-dropdown-menu class="profile-menu">
        <el-dropdown-item command="lang">
          <Languages :size="15" aria-hidden="true" />
          {{ t('langToggle', ui.lang) }}
          <span class="profile-value">{{
            ui.lang === 'zh' ? '中文' : 'English'
          }}</span>
        </el-dropdown-item>
        <el-dropdown-item divided command="chat">
          <MessageSquare :size="15" aria-hidden="true" />
          {{ t('goToChat', ui.lang) }}
        </el-dropdown-item>
        <el-dropdown-item command="logout">
          <LogOut :size="15" aria-hidden="true" />
          {{ t('logout', ui.lang) }}
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>

  <ConfirmDialog
    v-model="confirmOpen"
    :title="t('logoutConfirmTitle', ui.lang)"
    :confirm-text="t('logout', ui.lang)"
    :cancel-text="t('cancel', ui.lang)"
    danger
    :loading="logouting"
    @confirm="doLogout"
  >
    <template #impact>{{ t('logoutConfirmBody', ui.lang) }}</template>
  </ConfirmDialog>
</template>

<style scoped>
.profile-role {
  display: inline-block;
  margin-left: var(--sp-2);
  padding: 0 6px;
  border-radius: var(--r-full);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  line-height: 16px;
}
</style>
