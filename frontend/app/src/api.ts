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

const BASE = import.meta.env.VITE_API_BASE_URL || ''

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init.headers },
    })
  } catch {
    throw new Error('无法连接后端，请确认 FastAPI 已启动。')
  }
  if (!response.ok) {
    if (response.status === 502 || response.status === 503) throw new Error('无法连接后端，请确认 FastAPI 已启动。')
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail) ? detail.map((item: { msg?: string }) => item.msg).join('；') : null
    throw new ApiError(message || `请求失败（HTTP ${response.status}）`, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
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
