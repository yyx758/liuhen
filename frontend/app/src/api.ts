export type Task = {
  id: number
  title: string
  priority: 'low' | 'medium' | 'high'
  start_at: string | null
  end_at: string | null
  duration_minutes: number | null
  due_at: string | null
  status: 'todo' | 'done'
  version: number
  /** 提前多少分钟提醒；null 表示不提醒，0 表示准时。 */
  remind_before_minutes: number | null
  created_by_agent?: boolean
}

export type TaskInput = Pick<Task, 'title' | 'priority' | 'start_at' | 'duration_minutes' | 'due_at' | 'remind_before_minutes'>
export type TaskUpdate = Partial<Pick<Task, 'title' | 'priority' | 'start_at' | 'duration_minutes' | 'due_at' | 'status' | 'remind_before_minutes'>>

export type Action = {
  id: number
  task_id: number
  action_type: 'update' | 'delete'
  before: Task
  after: Task | null
  status: 'pending' | 'executed' | 'cancelled' | 'expired' | 'conflicted' | 'replaced' | 'failed'
  created_at: string
  expires_at: string
  request_id?: string | null
}

export type AuditEvent = {
  id: number
  event_type: string
  task_id: number
  action_id: number | null
  request_id: string | null
  conversation_id: string | null
  tool_call_id: string | null
  from_status: string | null
  to_status: string | null
  reason: string | null
  details: Record<string, unknown> | null
  created_at: string
  subject_title: string | null
}

export type AuditSummary = {
  total: number
  proposals: number
  executed: number
  cancelled: number
  expired: number
  failures: number
  requests: number
}

export type AuditEventPage = {
  items: AuditEvent[]
  page: number
  page_size: number
  total: number
  total_pages: number
  anchor_id: number | null
}

export type EventQuery = {
  limit?: number
  beforeId?: number
  actionId?: number
  requestId?: string
  since?: number
  category?: 'all' | 'failed' | 'cancelled'
}

export type CurrentUser = {
  id: number
  username: string
  email: string | null
  /** 邮箱是否已通过验证码验证；只有已验证的邮箱能找回密码。 */
  email_verified: boolean
  /** 是否接收提醒邮件；邮箱未验证时无论开关都不发。 */
  email_reminders: boolean
  role: 'user' | 'admin'
  must_change_password: boolean
  created_at: string
  /** 今日对话用量；limit 为 null 表示不限量（管理员）。 */
  chat_usage?: { today: number; limit: number | null } | null
}

export type UserStatus = 'pending' | 'active' | 'rejected' | 'disabled'

/** 页面内通知；missed 表示提醒到点时服务不可用、超过 1 小时才处理。 */
export type AppNotification = {
  id: number
  kind: 'reminder' | 'missed'
  task_id: number
  task_title: string
  task_time: string
  created_at: string
  read_at: string | null
}

/** 发送验证码成功：expires_in 秒内有效，resend_after 秒后可以重新获取。 */
export type EmailCodeSent = { expires_in: number; resend_after: number }

export type LoginSession = {
  id: number
  created_at: string
  last_seen_at: string
  ip: string | null
  user_agent: string | null
  current: boolean
}

export type OwnEvent = { id: number; event_type: string; ip: string | null; created_at: string }

export type AdminUser = {
  id: number
  username: string
  email: string | null
  email_verified: boolean
  role: 'user' | 'admin'
  status: UserStatus
  must_change_password: boolean
  created_at: string
  last_login_at: string | null
  reviewed_at: string | null
  status_reason: string | null
  daily_chat_limit: number | null
  chat_limit: number | null
  chat_today: number
  chat_week: number
  chat_days?: number[] | null
}

export type AdminUserPage = {
  items: AdminUser[]
  total: number
  page: number
  page_size: number
  counts: Record<UserStatus | 'all', number>
  chat_today: number
  chat_week: number
  default_chat_limit: number
}

export type AccountEvent = {
  id: number
  event_type: string
  target_user_id: number
  actor_user_id: number | null
  target_username: string | null
  actor_username: string | null
  reason: string | null
  details: Record<string, unknown> | null
  created_at: string
}

export type AccountEventGroup = 'all' | 'review' | 'account' | 'login'

const BASE = import.meta.env.VITE_API_BASE_URL || ''

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

// 会话失效（401）时的统一处理（跳转登录页）；由 main.ts 注册，避免这里直接依赖路由。
let authErrorHandler: ((error: ApiError) => void) | null = null
export function onAuthError(handler: (error: ApiError) => void) { authErrorHandler = handler }

type RequestOptions = RequestInit & { /** 登录、获取当前用户等接口自己处理 401，不触发统一跳转。 */ handleAuthError?: boolean }

async function request<T>(path: string, { handleAuthError = true, ...init }: RequestOptions = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      // 会话令牌在 HttpOnly Cookie 中，由浏览器自动携带；前端代码读不到也不保存令牌。
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...init.headers },
    })
  } catch {
    throw new Error('无法连接后端，请确认 FastAPI 已启动。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    // 反向代理连不上后端时也返回 502/503，但没有后端的 detail；后端自己返回的 502/503（如邮件发送失败）带 detail。
    if ((response.status === 502 || response.status === 503) && typeof detail !== 'string') {
      throw new Error('无法连接后端，请确认 FastAPI 已启动。')
    }
    let message: string | null = null
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail)) message = detail.map((item: { msg?: string }) => item.msg).join('；')
    const error = new ApiError(message || `请求失败（HTTP ${response.status}）`, response.status)
    if (handleAuthError && response.status === 401) authErrorHandler?.(error)
    throw error
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  me: () => request<CurrentUser>('/auth/me', { handleAuthError: false }),
  login: (account: string, password: string, remember: boolean) => request<CurrentUser>('/auth/login', {
    method: 'POST', body: JSON.stringify({ account, password, remember }), handleAuthError: false,
  }),
  logout: () => request<void>('/auth/logout', { method: 'POST', handleAuthError: false }),
  registerEmailCode: (email: string) => request<EmailCodeSent>('/auth/register/email-code', {
    method: 'POST', body: JSON.stringify({ email }), handleAuthError: false,
  }),
  register: (username: string, password: string, email: string, code: string) => request<{ username: string; status: UserStatus }>('/auth/register', {
    method: 'POST', body: JSON.stringify({ username, password, email, code }), handleAuthError: false,
  }),
  passwordResetEmailCode: (email: string) => request<EmailCodeSent>('/auth/password-reset/email-code', {
    method: 'POST', body: JSON.stringify({ email }), handleAuthError: false,
  }),
  resetPassword: (email: string, code: string, newPassword: string) => request<void>('/auth/password-reset', {
    method: 'POST', body: JSON.stringify({ email, code, new_password: newPassword }), handleAuthError: false,
  }),
  ownEmailCode: (email: string, currentPassword: string) => request<EmailCodeSent>('/auth/me/email-code', {
    method: 'POST', body: JSON.stringify({ email, current_password: currentPassword }),
  }),
  /** email 为 null 表示清除邮箱，此时不需要验证码。 */
  changeEmail: (email: string | null, code: string | null, currentPassword: string) => request<CurrentUser>('/auth/me', {
    method: 'PATCH', body: JSON.stringify({ email, code, current_password: currentPassword }),
  }),
  setPreferences: (emailReminders: boolean) => request<CurrentUser>('/auth/me/preferences', {
    method: 'PATCH', body: JSON.stringify({ email_reminders: emailReminders }),
  }),
  notifications: (limit = 20) => request<AppNotification[]>(`/notifications?limit=${limit}`),
  unreadNotifications: () => request<{ count: number }>('/notifications/unread-count'),
  readNotification: (id: number) => request<void>(`/notifications/${id}/read`, { method: 'POST' }),
  readAllNotifications: () => request<void>('/notifications/read-all', { method: 'POST' }),
  sessions: () => request<LoginSession[]>('/auth/sessions'),
  revokeSession: (id: number) => request<void>(`/auth/sessions/${id}`, { method: 'DELETE' }),
  revokeOtherSessions: () => request<{ revoked: number }>('/auth/sessions/revoke-others', { method: 'POST' }),
  ownEvents: () => request<OwnEvent[]>('/auth/events'),
  adminUsers: (status: UserStatus | 'all', q: string, page = 1, pageSize = 100) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (status !== 'all') params.set('status', status)
    if (q.trim()) params.set('q', q.trim())
    return request<AdminUserPage>(`/admin/users?${params}`)
  },
  adminUser: (id: number) => request<AdminUser>(`/admin/users/${id}`),
  setChatLimit: (id: number, limit: number | null) => request<AdminUser>(`/admin/users/${id}`, {
    method: 'PATCH', body: JSON.stringify({ daily_chat_limit: limit }),
  }),
  pendingUserCount: () => request<{ count: number }>('/admin/users/pending-count'),
  approveUser: (id: number) => request<AdminUser>(`/admin/users/${id}/approve`, { method: 'POST' }),
  rejectUser: (id: number, reason: string) => request<AdminUser>(`/admin/users/${id}/reject`, {
    method: 'POST', body: JSON.stringify({ reason: reason || null }),
  }),
  disableUser: (id: number, reason: string) => request<AdminUser>(`/admin/users/${id}/disable`, {
    method: 'POST', body: JSON.stringify({ reason: reason || null }),
  }),
  enableUser: (id: number) => request<AdminUser>(`/admin/users/${id}/enable`, { method: 'POST' }),
  resetUserPassword: (id: number) => request<{ temporary_password: string }>(`/admin/users/${id}/reset-password`, { method: 'POST' }),
  deleteUser: (id: number) => request<void>(`/admin/users/${id}`, { method: 'DELETE' }),
  accountEvents: ({ userId, group = 'all', beforeId, limit = 50 }: { userId?: number; group?: AccountEventGroup; beforeId?: number; limit?: number } = {}) => {
    const params = new URLSearchParams({ group, limit: String(limit) })
    if (userId !== undefined) params.set('user_id', String(userId))
    if (beforeId !== undefined) params.set('before_id', String(beforeId))
    return request<AccountEvent[]>(`/admin/account-events?${params}`)
  },
  changePassword: (oldPassword: string, newPassword: string) => request<void>('/auth/password', {
    method: 'POST', body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }), handleAuthError: false,
  }),
  tasks: () => request<Task[]>('/tasks'),
  createTask: (input: TaskInput, key: string) => request<Task>('/tasks', {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(input),
  }),
  updateTask: (id: number, version: number, input: TaskUpdate) => request<Task>(`/tasks/${id}`, {
    method: 'PATCH', headers: { 'If-Match': String(version) }, body: JSON.stringify(input),
  }),
  deleteTask: (id: number, version: number) => request<void>(`/tasks/${id}`, {
    method: 'DELETE', headers: { 'If-Match': String(version) },
  }),
  action: (id: number) => request<Action>(`/actions/${id}`),
  pendingActions: () => request<Action[]>('/actions/pending'),
  conversationActions: (conversationId: string) => request<Action[]>(`/actions/conversations/${encodeURIComponent(conversationId)}`),
  confirm: (id: number) => request<Action>(`/actions/${id}/confirm`, { method: 'POST' }),
  cancel: (id: number) => request<Action>(`/actions/${id}/cancel`, { method: 'POST' }),
  deleteExpired: (id: number) => request<void>(`/actions/${id}`, { method: 'DELETE' }),
  events: ({ limit = 20, beforeId, actionId, requestId, since, category }: EventQuery = {}) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (beforeId !== undefined) params.set('before_id', String(beforeId))
    if (actionId !== undefined) params.set('action_id', String(actionId))
    if (requestId !== undefined) params.set('request_id', requestId)
    if (since !== undefined) params.set('since', String(since))
    if (category && category !== 'all') params.set('category', category)
    return request<AuditEvent[]>(`/audit/events?${params}`)
  },
  eventPage: (page: number, pageSize: number, since?: number, category: 'all' | 'failed' | 'cancelled' = 'all', anchorId?: number) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), category })
    if (since !== undefined) params.set('since', String(since))
    if (anchorId !== undefined) params.set('anchor_id', String(anchorId))
    return request<AuditEventPage>(`/audit/events/page?${params}`)
  },
  auditSummary: (since?: number) => request<AuditSummary>(`/audit/summary${since === undefined ? '' : `?since=${since}`}`),
  chat: (userText: string, requestId: string, conversationId: string | null) =>
    request<{ answer: string; conversation_id: string; request_id: string }>('/chat', {
      method: 'POST', body: JSON.stringify({ user_text: userText, request_id: requestId, conversation_id: conversationId }),
    }),
  deleteConversation: (conversationId: string) => request<void>(`/chat/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
  }),
}
