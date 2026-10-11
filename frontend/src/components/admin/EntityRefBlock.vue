<!--
  提案对象块(O4):「针对 贷款 L456（customer：张伟）」。

  行动台三处(列表行 / 详情抽屉 / 批量审批预览)共用**这一个组件** ——
  三处各写一遍迟早漂移(NF6)。形状守卫与正文格式在 utils/entityRef
  单一处;这里只把「针对」标签与正文拼起来,不改一个字符的数据,也
  不做任何补算(键缺席处根本不渲染本组件)。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { formatEntityRef } from '../../utils/entityRef'
import type { EntityRefInfo } from '../../api/types'

const props = defineProps<{ entityRef: EntityRefInfo }>()

const ui = useUiStore()

/** 标签 + 正文:「针对 贷款 L456（customer：张伟）」。 */
const text = computed(
  () => `${t('actionEntityTarget', ui.lang)} ${formatEntityRef(props.entityRef, ui.lang)}`,
)
</script>

<template>
  <span class="entity-ref">{{ text }}</span>
</template>

<style scoped>
.entity-ref {
  white-space: normal;
  overflow-wrap: anywhere;
}
</style>
