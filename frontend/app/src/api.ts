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
  created_by_agent?: boolean
}

export type TaskInput = Pick<Task, 'title' | 'priority' | 'start_at' | 'duration_minutes' | 'due_at'>
export type TaskUpdate = Partial<Pick<Task, 'title' | 'priority' | 'start_at' | 'duration_minutes' | 'due_at' | 'status'>>

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
  role: 'user' | 'admin'
  must_change_password: boolean
}

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
    if (response.status === 502 || response.status === 503) throw new Error('无法连接后端，请确认 FastAPI 已启动。')
    const body = await response.json().catch(() => null)
    const detail = body?.detail
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
