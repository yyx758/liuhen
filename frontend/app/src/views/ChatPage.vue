<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { api, type Action } from '../api'
import { auth, refreshUsage } from '../auth'
import { proposalChanges } from '../format'
import { renderMarkdown } from '../markdown'
import { activeChat, newChat, refresh, saveChats, sendChat, state, type ChatMessage, type LocalChat } from '../state'
import Icon from '../components/Icon.vue'

if (!state.activeChatId) state.activeChatId = state.chats[0]?.id || newChat().id
const chat = computed(() => activeChat())
const proposals = ref<Action[]>([])
const input = ref('')
const scroll = ref<HTMLElement | null>(null)
const pendingFor = (message: ChatMessage) => message.role === 'assistant' ? proposals.value.filter(action =>
  action.status !== 'expired' && (action.status !== 'pending' || new Date(action.expires_at).getTime() > state.clockNow) &&
  action.request_id === message.requestId) : []

const toolLabels: Record<string, string> = {
  create_task: '正在创建任务…', list_tasks: '正在查询任务…', get_task: '正在读取任务详情…',
  get_action: '正在查询提案状态…', propose_update: '正在生成修改提案…', propose_delete: '正在生成删除提案…',
}
const previewStatus = computed(() => toolLabels[state.chatPreview.tool ?? ''] || '正在处理…')

watch(() => [chat.value.messages.length, state.chatPreview.text], async () => { await nextTick(); if (scroll.value) scroll.value.scrollTop = scroll.value.scrollHeight })
let proposalLoad = 0
async function loadProposals(conversationId: string | null) {
  const load = ++proposalLoad
  proposals.value = []
  if (!conversationId) return
  try {
    const result = await api.conversationActions(conversationId)
    if (load === proposalLoad) proposals.value = result
  } catch (error) {
    if (load === proposalLoad) state.error = error instanceof Error ? error.message : '加载提案失败'
  }
}
watch(() => state.activeChatId, () => { void loadProposals(chat.value.conversationId) }, { immediate: true })
watch(() => state.events[0]?.id, (id, previous) => {
  if (id !== previous) void loadProposals(chat.value.conversationId)
})
async function submit() {
  const text = input.value.trim()
  if (!text || state.busy) return
  input.value = ''
  await sendChat(text)
  void refreshUsage()
}
const usageText = computed(() => {
  const usage = auth.user?.chat_usage
  if (!usage) return ''
  return usage.limit === null ? `今日已用 ${usage.today} 轮 · 不限量` : `今日已用 ${usage.today} / ${usage.limit} 轮`
})
const usageFull = computed(() => {
  const usage = auth.user?.chat_usage
  return Boolean(usage && usage.limit !== null && usage.today >= usage.limit)
})
function openChat(id: string) { state.activeChatId = id; saveChats() }
async function removeChat(item: LocalChat) {
  if (state.busy || !window.confirm(`删除「${item.title}」的会话记录？任务和操作留痕会保留。`)) return
  state.busy = true
  state.error = ''
  try {
    if (item.conversationId) await api.deleteConversation(item.conversationId)
    const index = state.chats.findIndex(chat => chat.id === item.id)
    if (index >= 0) state.chats.splice(index, 1)
    if (state.activeChatId === item.id) state.activeChatId = state.chats[0]?.id || newChat().id
    saveChats()
    state.toast = '会话记录已删除。'
  } catch (error) { state.error = error instanceof Error ? error.message : '删除会话失败' }
  finally { state.busy = false }
}
async function retry(message: ChatMessage) { if (!state.busy) { await sendChat(message.text, message); void refreshUsage() } }
async function decide(id: number, decision: 'confirm' | 'cancel') {
  state.busy = true
  state.error = ''
  try {
    const action = await (decision === 'confirm' ? api.confirm(id) : api.cancel(id))
    state.toast = action.status === 'executed' ? '提案已执行。' : action.status === 'cancelled' ? '提案已取消。' : `提案状态：${action.status}`
    await refresh()
  } catch (error) { state.error = error instanceof Error ? error.message : '操作失败' }
  finally { state.busy = false }
}
</script>

<template>
  <main class="page chat-page"><div class="page-head"><div><div class="page-kicker"><span class="dot" /> ASSISTANT</div><h1 class="page-title">和小记聊聊</h1><p class="lead">用一句话安排任务，重要修改由你确认。</p></div></div>
    <div class="chat-layout"><aside class="chat-sidebar"><button class="chat-start" type="button" @click="newChat()"><Icon name="plus" :size="17" />开启新对话</button><div class="chat-group-label">最近的对话</div><div v-for="item in state.chats" :key="item.id" class="history-row-chat"><button class="history-btn" :class="{ active: item.id === state.activeChatId }" type="button" @click="openChat(item.id)">{{ item.title }}</button><button class="history-delete" type="button" :disabled="state.busy" :aria-label="`删除会话：${item.title}`" :title="`删除会话：${item.title}`" @click="removeChat(item)"><Icon name="trash" :size="15" /></button></div><div class="chat-about">会话同步保存在浏览器与后端。<br />删除会话不影响任务和操作留痕。</div></aside>
      <section class="chat-main"><div class="chat-bar"><span>{{ chat.title }}</span><div style="display:flex;align-items:center;gap:12px"><span class="chat-badge">AI 对话</span><button class="chat-mobile-delete" type="button" :disabled="state.busy" aria-label="删除当前会话" @click="removeChat(chat)"><Icon name="trash" :size="16" /></button></div></div><div ref="scroll" class="chat-messages"><div v-if="!chat.messages.length" class="empty-state" style="margin:auto"><div class="empty-symbol"><Icon name="message" :size="23" /></div><h3>想到什么，就告诉小记</h3><p>试着输入“明天下午三点提醒我复习 Redis”</p></div>
        <div v-for="(message, index) in chat.messages" :key="index" class="chat-msg" :class="{ user: message.role === 'user' }"><span class="msg-avatar">{{ message.role === 'user' ? '我' : '✳' }}</span><div class="message-body"><div class="message-name">{{ message.role === 'user' ? '你' : '小记' }}</div><div v-if="message.role === 'assistant'" class="bubble markdown-content" v-html="renderMarkdown(message.text)" /><div v-else class="bubble">{{ message.text }}</div><button v-if="message.failed" class="subtle-link retry-link" type="button" :disabled="state.busy" @click="retry(message)">发送失败，使用原请求重试</button><div v-for="action in pendingFor(message)" :key="action.id" class="suggestion"><div class="diff-title">{{ action.action_type === 'delete' ? '删除提案' : '修改提案' }} · #{{ action.id }}</div><b>{{ action.before.title }}</b><div v-for="change in proposalChanges(action)" :key="change.label" class="proposal-change"><div class="tiny">{{ change.label }}</div><div class="diff-columns"><div class="diff-col"><span class="tiny">修改前</span><b>{{ change.before }}</b></div><span class="diff-arrow">→</span><div class="diff-col new"><span class="tiny">修改后</span><b>{{ change.after }}</b></div></div></div><div v-if="action.status === 'pending'" class="diff-actions"><button class="solid" type="button" :disabled="state.busy" @click="decide(action.id, 'confirm')">确认执行</button><button class="outline" type="button" :disabled="state.busy" @click="decide(action.id, 'cancel')">取消</button></div><span v-else class="tiny">{{ action.status }}</span></div></div></div>
        <div v-if="state.busy" class="chat-msg"><span class="msg-avatar">✳</span><div class="message-body"><div class="message-name">小记</div><div v-if="state.chatPreview.text" class="bubble markdown-content" v-html="renderMarkdown(state.chatPreview.text)" /><div v-else class="bubble">{{ previewStatus }}</div></div></div>
      </div><div class="chat-compose"><form class="chat-input-wrap" @submit.prevent="submit"><textarea v-model="input" rows="2" placeholder="输入你想安排的事情…" aria-label="输入消息" @keydown.enter.exact.prevent="submit" /><button class="chat-send" type="submit" :disabled="state.busy || !input.trim()" aria-label="发送消息"><Icon name="up" :size="17" /></button></form><div class="chat-hint">Enter 发送 · Shift + Enter 换行 · 修改和删除需你确认<span v-if="usageText" class="chat-usage" :class="{ full: usageFull }"> · {{ usageFull ? '今日对话次数已用完，明天再来' : usageText }}</span></div></div></section></div>
  </main>
</template>
