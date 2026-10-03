<!--
  主题域选择器(问数范围收敛:不限定 / 某域)—— 排在数据源选择器同一行。

  三件事只在这里表达:
  · **无域可选 → 整体不渲染**(空清单 = 零噪音,不是「选择器禁用」);
  · **过期域置灰标「已失效」** —— 选了必然被拒,置灰挡在手滑之前;域照常
    列出而不是消失,因为静默消失会让用户带着一个选不中的旧值继续提问;
  · 域名/描述/示例是 KB 原文,照显不译(i18n 只包 UI 文案)。
-->
<template>
  <template v-if="ui.activeTopics.length">
    <span class="chat-ds-sep" />
    <div class="chat-ds-label">
      <Layers :size="14" :stroke-width="2" />
      <span>{{ t('topicLabel', ui.lang) }}</span>
    </div>
    <el-select
      class="chat-topic-select"
      size="small"
      :model-value="ui.activeTopic"
      @change="onChange"
    >
      <el-option :value="''" :label="t('topicAny', ui.lang)" />
      <el-option
        v-for="tp in ui.activeTopics"
        :key="tp.name"
        :value="tp.name"
        :label="tp.name"
        :disabled="tp.status !== 'ok'"
      >
        <span class="ds-opt-name">{{ tp.name }}</span>
        <span v-if="tp.description" class="ds-opt-desc">{{ tp.description }}</span>
        <span
          v-if="tp.status === 'ok' && tp.scope.length"
          class="topic-opt-scope"
          :title="tp.scope.join('、')"
        >{{ t('topicScopeTables', ui.lang, tp.scope.length) }}</span>
        <span v-else-if="tp.status !== 'ok'" class="topic-opt-stale">
          {{ t('topicStale', ui.lang) }}
        </span>
      </el-option>
    </el-select>
  </template>
</template>

<script setup lang="ts">
import { Layers } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const ui = useUiStore()

/** el-select 的 payload 类型是宽联合;收敛成 string 再进 store。 */
function onChange(value: unknown) {
  ui.setTopic(String(value ?? ''))
}
</script>
