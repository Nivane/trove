<template>
  <div class="admin-shell">
    <div class="admin-sidebar">
      <div class="brand"><span class="brand-mark">◆</span> Trove</div>
      <el-menu :default-active="$route.path" router class="admin-menu">
        <el-menu-item index="/admin/users">{{ t('users', ui.lang) }}</el-menu-item>
        <el-menu-item index="/admin/kb">{{ t('kb', ui.lang) }}</el-menu-item>
        <el-menu-item index="/admin/audit">{{ t('audit', ui.lang) }}</el-menu-item>
      </el-menu>
      <div class="admin-footer">
        <router-link to="/" class="back-link">← {{ t('brand', ui.lang) }}</router-link>
        <button class="logout-btn" @click="logout">{{ t('logout', ui.lang) }}</button>
      </div>
    </div>
    <div class="admin-content">
      <router-view />
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { useRouter } from 'vue-router'
import { t } from '../i18n'

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()

async function logout() {
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>
