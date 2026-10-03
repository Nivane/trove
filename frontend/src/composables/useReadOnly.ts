/**
 * useReadOnly — W5 阶段二「只读态」的单一判定（设计稿 §2.2 R3）。
 *
 * readOnly = 当前角色不是 admin（analyst 进管理台即只读；user 角色进不来，
 * 走到这里也按只读处理 —— 方向安全）。写按钮 / 复核条 / 表单按它隐藏：
 * 后端对这些端点仍是 403（全部写端点保持 require_admin），前端隐藏是体验
 * 层 —— 「改不动」的正确形态是看不见按钮，而不是点开吃 403。
 */
import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'
import { canAccess, requiredRoles, router } from '../router'

export function useReadOnly() {
  const auth = useAuthStore()
  const readOnly = computed(() => auth.user?.role !== 'admin')

  /**
   * 该角色能否打开这个站内路径（与路由守卫同一份判定：meta.roles 契约）。
   * 页面里指向别处的链接/深链在渲染前过这一道 —— 只读用户的页面上
   * 不留「点了被弹回」的死链（R1：能看见 ⟺ 点得开）。
   */
  function canOpen(path: string): boolean {
    return canAccess(auth.user?.role, requiredRoles(router.resolve(path)))
  }

  return { readOnly, canOpen }
}
