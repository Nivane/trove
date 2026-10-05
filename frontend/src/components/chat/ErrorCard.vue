<template>
  <div class="error-card">
    <div class="error-head">
      <AlertCircle :size="15" class="error-icon" />
      <span class="error-title">{{ card.title }}</span>
    </div>
    <p v-if="card.explanation" class="error-text">{{ card.explanation }}</p>
    <p v-if="card.suggestion" class="error-hint">{{ card.suggestion }}</p>

    <!-- 技术细节:节点名 / 错误类别 / 原文。仅管理员可见,默认折叠。 -->
    <details v-if="auth.isAdmin && card.detail.raw" class="error-detail">
      <summary>{{ t('techDetail', ui.lang) }}</summary>
      <dl class="error-detail-list">
        <template v-if="card.detail.nodeLabel">
          <dt>{{ t('errorNode', ui.lang) }}</dt>
          <dd>{{ card.detail.nodeLabel }}</dd>
        </template>
        <template v-if="card.detail.errorClass">
          <dt>{{ t('errorClass', ui.lang) }}</dt>
          <dd class="mono">
            {{ card.detail.errorClass
            }}<template v-if="card.detail.domain"> · {{ card.detail.domain }}</template>
          </dd>
        </template>
        <dt>{{ t('errorRaw', ui.lang) }}</dt>
        <dd class="mono">{{ card.detail.raw }}</dd>
      </dl>
    </details>

    <div class="error-actions">
      <button
        v-if="card.retryable"
        class="error-btn error-btn-primary"
        @click="emit('retry')"
      >
        <RefreshCw :size="13" />
        {{ t('retry', ui.lang) }}
      </button>
      <button class="error-btn" @click="emit('rephrase')">
        <Pencil :size="13" />
        {{ t('rephrase', ui.lang) }}
      </button>
      <!-- 后端驱动的深链出口(有管理端落点的类别才有;admin_only 过角色闸) -->
      <RouterLink
        v-for="a in visibleExits"
        :key="a.id ?? a.href"
        class="error-btn"
        :to="a.href!"
      >
        <ArrowRight :size="13" />
        {{ a.label }}
      </RouterLink>
      <button v-if="auth.isAdmin" class="error-btn" @click="emit('admin')">
        <Settings :size="13" />
        {{ t('gotoAdmin', ui.lang) }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { AlertCircle, ArrowRight, Pencil, RefreshCw, Settings } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { useAuthStore } from '../../stores/auth'
import { t } from '../../i18n'
import type { ErrorCardModel } from '../../utils/errors'

const props = defineProps<{ card: ErrorCardModel }>()
const emit = defineEmits<{
  retry: []
  rephrase: []
  admin: []
}>()

const ui = useUiStore()
const auth = useAuthStore()

// 渲染 + 角色闸而已,分类与文案全部来自后端(utils/errors.ts 纪律)。
const visibleExits = computed(() =>
  props.card.actions.filter((a) => !a.admin_only || auth.isAdmin),
)
</script>
