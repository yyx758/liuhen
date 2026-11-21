import { reactive } from 'vue'
import { api, type CurrentUser } from './api'
import { clearUserData, useUserData } from './state'

/** 当前登录用户。checked 为 false 时还没向后端确认过登录状态。 */
export const auth = reactive({
  user: null as CurrentUser | null,
  checked: false,
})

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
