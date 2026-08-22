<template>
  <div class="login-view">
    <div class="login-card">
      <div class="login-brand">
        <span class="brand-mark"
          ><el-icon :size="14"><DataAnalysis /></el-icon
        ></span>
        Trove
      </div>
      <h2 class="login-title">{{ t('loginTitle', ui.lang) }}</h2>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input
            v-model="username"
            :placeholder="t('loginUser', ui.lang)"
            autofocus
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="password"
            type="password"
            :placeholder="t('loginPass', ui.lang)"
            show-password
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-alert
          v-if="error"
          type="error"
          :title="t('loginFail', ui.lang)"
          show-icon
          :closable="false"
          class="login-error"
        />
        <el-button
          type="primary"
          class="login-btn"
          :loading="loading"
          @click="submit"
        >
          {{ t('loginBtn', ui.lang) }}
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { DataAnalysis } from '@element-plus/icons-vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { t } from '../i18n'

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref(false)

async function submit() {
  if (!username.value || !password.value || loading.value) return
  loading.value = true
  error.value = false
  try {
    await auth.login(username.value, password.value)
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await router.push(next)
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}
</script>
