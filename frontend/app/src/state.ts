import { reactive } from 'vue'
import { api, type Action, type AuditEvent, type Task } from './api'

export type ChatMessage = { role: 'user' | 'assistant'; text: string; requestId?: string; failed?: boolean }
export type LocalChat = { id: string; conversationId: string | null; title: string; messages: ChatMessage[] }

// 本地对话列表按用户分开保存，同一浏览器切换账号时看不到上一个账号的对话。
// 多用户改造前的旧键直接丢弃（服务端数据已重建，旧的 conversation_id 已无效）。
const LEGACY_CHAT_KEY = 'liuhen.frontend.chats.v1'
let chatKey: string | null = null

function readChats(key: string): LocalChat[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || '[]')
    return Array.isArray(value) ? value as LocalChat[] : []
  } catch { return [] }
}

export const state = reactive({
  tasks: [] as Task[],
  actions: [] as Action[],
  events: [] as AuditEvent[],
  chats: [] as LocalChat[],
  activeChatId: null as string | null,
  loading: false,
  busy: false,
  clockNow: Date.now(),
  error: '',
  toast: '',
  editorTaskId: null as number | null,
  editorOpen: false,
  detailTaskId: null as number | null,
  /** 正在生成的回复预览：text 为模型已输出的文字，tool 为正在调用的工具；仅用于展示，以 done 的回复为准。 */
  chatPreview: { text: '', tool: null as string | null },
})

export function saveChats() {
  if (!chatKey) return
  try { localStorage.setItem(chatKey, JSON.stringify(state.chats)) } catch { /* private browsing */ }
}

/** 清空内存中属于上一个用户的数据（登出、会话失效时调用）。 */
export function clearUserData() {
  chatKey = null
  Object.assign(state, {
    tasks: [], actions: [], events: [], chats: [], activeChatId: null,
    error: '', toast: '', editorTaskId: null, editorOpen: false, detailTaskId: null,
    chatPreview: { text: '', tool: null },
  })
}

/** 切换到某个用户的数据：先清空，再读取该用户的本地对话列表。 */
export function useUserData(userId: number) {
  clearUserData()
  chatKey = `liuhen.chats.${userId}`
  state.chats = readChats(chatKey)
  try { localStorage.removeItem(LEGACY_CHAT_KEY) } catch { /* private browsing */ }
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
  state.chatPreview = { text: '', tool: null }
  try {
    const response = await api.chatStream(text, requestId, chat.conversationId, progress => {
      if (progress.event === 'delta') {
        state.chatPreview.text += progress.text
      } else {
        // 模型决定调用工具：之前的文字只是过渡语，清空后显示工具状态。
        state.chatPreview = { text: '', tool: progress.name }
      }
    })
    chat.conversationId = response.conversation_id
    chat.messages.push({ role: 'assistant', text: response.answer, requestId })
    // 已保存的回复取代预览，避免刷新任务列表期间同一段文字显示两遍。
    state.chatPreview = { text: '', tool: null }
    saveChats()
    await refresh()
    return true
  } catch (error) {
    message.failed = true
    saveChats()
    state.error = error instanceof Error ? error.message : '发送失败'
    return false
  } finally {
    state.busy = false
    state.chatPreview = { text: '', tool: null }
  }
}
