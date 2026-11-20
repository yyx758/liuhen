import { reactive } from 'vue'
import { api, type Action, type AuditEvent, type Task } from './api'

export type ChatMessage = { role: 'user' | 'assistant'; text: string; requestId?: string; failed?: boolean }
export type LocalChat = { id: string; conversationId: string | null; title: string; messages: ChatMessage[] }

const CHAT_KEY = 'liuhen.frontend.chats.v1'
function readChats(): LocalChat[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(CHAT_KEY) || '[]')
    return Array.isArray(value) ? value as LocalChat[] : []
  } catch { return [] }
}

export const state = reactive({
  tasks: [] as Task[],
  actions: [] as Action[],
  events: [] as AuditEvent[],
  chats: readChats(),
  activeChatId: null as string | null,
  loading: false,
  busy: false,
  clockNow: Date.now(),
  error: '',
  toast: '',
  editorTaskId: null as number | null,
  editorOpen: false,
  detailTaskId: null as number | null,
})

export function saveChats() {
  try { localStorage.setItem(CHAT_KEY, JSON.stringify(state.chats)) } catch { /* private browsing */ }
}

export function newChat(): LocalChat {
  const chat: LocalChat = { id: crypto.randomUUID(), conversationId: null, title: '新对话', messages: [] }
  state.chats.unshift(chat)
  state.activeChatId = chat.id
  saveChats()
  return chat
}

export function activeChat(): LocalChat {
  return state.chats.find(chat => chat.id === state.activeChatId) || newChat()
}

export async function refresh() {
  state.loading = true
  state.error = ''
  try {
    const [tasks, actions, events] = await Promise.all([
      api.tasks(), api.pendingActions(), api.events({ limit: 3 }),
    ])
    state.tasks = tasks
    state.events = events
    state.actions = actions
  } catch (error) {
    state.error = error instanceof Error ? error.message : '加载失败'
  } finally { state.loading = false }
}

export async function runAction(operation: () => Promise<unknown>, success: string) {
  state.busy = true
  state.error = ''
  try {
    await operation()
    state.toast = success
    await refresh()
    return true
  } catch (error) {
    state.error = error instanceof Error ? error.message : '操作失败'
    return false
  } finally { state.busy = false }
}

export async function sendChat(text: string, retry?: ChatMessage) {
  const chat = activeChat()
  const requestId = retry?.requestId || crypto.randomUUID()
  const message = retry || { role: 'user' as const, text, requestId }
  message.failed = false
  if (!retry) chat.messages.push(message)
  if (chat.messages.length === 1) chat.title = text.slice(0, 22)
  saveChats()
  state.busy = true
  state.error = ''
  try {
    const response = await api.chat(text, requestId, chat.conversationId)
    chat.conversationId = response.conversation_id
    chat.messages.push({ role: 'assistant', text: response.answer, requestId })
    saveChats()
    await refresh()
    return true
  } catch (error) {
    message.failed = true
    saveChats()
    state.error = error instanceof Error ? error.message : '发送失败'
    return false
  } finally { state.busy = false }
}
