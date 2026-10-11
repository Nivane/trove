<template>
  <!-- 已声明的链接(本体形态 O1,设计 §4 行 4 / §5.3):拒绝轮
       related_links 的只读投影 —— 一条 = 显示名 + 基数(「贷款 → 客户
       (属于 · 1:N)」)。

       三态如实,三态是三个不同的可区分事实:
        · 键缺席(undefined/null)= 后端还没投影这件事 → 整节不渲染;
        · 空数组 = 有声明面但**这个方向**一条链接都没有 → 引导文案
          (缺的是哪条,这里说清;已有的是哪些在关系声明页看);
        · 非空 = 逐条渲染。
       这里只渲染:不排序、不分类、不补默认显示名(显示名缺席就回退
       关系名,这是投影侧就定好的口径)。 -->
  <div v-if="links" class="refusal-links">
    <span class="refusal-links-label">{{ t('refusalRelatedLinks', ui.lang) }}</span>
    <ul v-if="rows.length" class="refusal-links-list">
      <li v-for="r in rows" :key="r.key" class="refusal-link-row">
        {{ r.text }}
      </li>
    </ul>
    <p v-else class="refusal-links-empty">{{ t('refusalRelatedLinksEmpty', ui.lang) }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import type { RelatedLinkInfo } from '../../api/types'

const props = defineProps<{
  /** 后端给的相关链接投影。缺席/null = 后端没给这件事(整节不渲染);
   *  空数组 = 给了,且该方向确实一条都没有(引导文案)。 */
  links?: RelatedLinkInfo[] | null
}>()

const ui = useUiStore()

interface Row {
  key: string
  text: string
}

/** 「from → to(显示名 · 基数)」——两段各自可缺,缺了就不摆空位。 */
const rows = computed<Row[]>(() =>
  (props.links ?? []).map((l, i) => {
    const key = l.name || `${l.from ?? ''}-${l.to ?? ''}-${i}`
    const path = [l.from, l.to].filter(Boolean).join(' → ')
    const detail = [l.display_name || l.name, l.cardinality].filter(Boolean).join(' · ')
    // 端点都缺时,括号里的细节就是全部内容 —— 不摆一个空的「→」。
    if (!path) return { key, text: detail || l.name || '' }
    return { key, text: detail ? `${path}${wrap(detail)}` : path }
  }),
)

/** 括号跟随界面语言(中文全角、英文半角)——基数记法本身逐字原样。 */
function wrap(s: string): string {
  return ui.lang === 'zh' ? `（${s}）` : ` (${s})`
}
</script>
