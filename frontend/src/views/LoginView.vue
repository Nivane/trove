<template>
  <div class="login-view">
    <div class="login-glow login-glow-a" />
    <div class="login-glow login-glow-b" />
    <div class="login-card-wrap">
      <div class="login-card">
        <div class="login-head">
          <span class="login-logo"><BrandMark :size="28" /></span>
          <h1 class="login-title">{{ t('loginTitle', ui.lang) }}</h1>
          <p class="login-subtitle">{{ t('loginSubtitle', ui.lang) }}</p>
        </div>

        <el-form @submit.prevent="submit">
          <div class="login-field">
            <span class="login-field-icon"><UserCircle :size="17" /></span>
            <el-input
              v-model="username"
              :placeholder="t('loginUser', ui.lang)"
              autofocus
              @keyup.enter="submit"
            />
          </div>
          <div class="login-field">
            <span class="login-field-icon"><Lock :size="17" /></span>
            <el-input
              v-model="password"
              type="password"
              :placeholder="t('loginPass', ui.lang)"
              show-password
              @keyup.enter="submit"
            />
          </div>

          <transition name="fade">
            <div v-if="error" class="login-error">
              <AlertCircle :size="15" />
              <span>{{ t('loginFail', ui.lang) }}</span>
            </div>
          </transition>

          <el-button
            type="primary"
            class="login-btn"
            :loading="loading"
            @click="submit"
          >
            {{ t('loginBtn', ui.lang) }}
          </el-button>
        </el-form>

        <div class="login-footer">
          <span class="login-hint">{{ t('adminOnlyHint', ui.lang) }}</span>
          <button class="login-lang" @click="toggleLang">
            <Languages :size="13" />
            {{ ui.lang === 'zh' ? 'English' : '中文' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { UserCircle, Lock, AlertCircle, Languages } from 'lucide-vue-next'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import BrandMark from '../components/brand/BrandMark.vue'
import { t } from '../i18n'

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref(false)

function toggleLang() {
  ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
}

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