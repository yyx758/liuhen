import { reactive } from 'vue'
import { api, type CurrentUser } from './api'
import { clearUserData, useUserData } from './state'

/** 当前登录用户。checked 为 false 时还没向后端确认过登录状态。 */
export const auth = reactive({
  user: null as CurrentUser | null,
  checked: false,
  /** 待审核账号数（只对管理员有意义），用于导航角标。 */
  pendingUsers: 0,
})

/** 发送对话后刷新今日用量（不影响其他用户状态）。 */
export async function refreshUsage() {
  if (!auth.user) return
  try {
    const user = await api.me()
    if (auth.user?.id === user.id) auth.user.chat_usage = user.chat_usage
  } catch { /* 用量刷新失败不影响对话 */ }
}

/** 刷新待审核角标；打开页面、切换页面和审核操作后调用，不做实时推送（D15）。 */
export async function refreshPendingUsers() {
  if (auth.user?.role !== 'admin' || auth.user.must_change_password) { auth.pendingUsers = 0; return }
  try { auth.pendingUsers = (await api.pendingUserCount()).count } catch { /* 角标失败不影响页面 */ }
}

/** 更新当前用户；换了人（包括登出）时清空上一个用户的数据。 */
export function setUser(user: CurrentUser | null) {
  const changed = auth.user?.id !== user?.id
  auth.user = user
  if (changed) {
    if (user) useUserData(user.id)
    else clearUserData()
  }
}

export async function loadCurrentUser(): Promise<CurrentUser | null> {
  try {
    setUser(await api.me())
  } catch {
    // 未登录（401）或后端不可用：都按未登录处理，登录页会显示具体错误。
    setUser(null)
  } finally {
    auth.checked = true
  }
  return auth.user
}

export async function login(account: string, password: string, remember: boolean) {
  setUser(await api.login(account, password, remember))
}

export async function logout() {
  try { await api.logout() } finally { setUser(null) }
}

/** 只接受站内路径，避免登录后被 ?redirect= 带到外部网站。 */
export function safeRedirect(value: unknown): string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') ? value : '/'
}
