<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, type Action, type AuditEvent, type AuditSummary } from '../api'
import { localParts, proposalChanges } from '../format'
import { refresh, state } from '../state'

type Filter = 'all' | 'pending' | 'failed' | 'cancelled'

const route = useRoute()
const filter = ref<Filter>(route.query.filter === 'pending' ? 'pending' : 'all')
watch(() => route.query.filter, value => { filter.value = value === 'pending' ? 'pending' : 'all' })
const thisWeek = ref(false)
const expandedId = ref<number | null>(null)
const auditEvents = ref<AuditEvent[]>([])
const related = ref<AuditEvent[]>([])
const summary = ref<AuditSummary | null>(null)
const pageLoading = ref(false)
const pageError = ref('')
const pageSize = 20
const currentPage = ref(1)
const jumpPage = ref(1)
const lastRequestedPage = ref(1)
const totalPages = ref(0)
const totalRecords = ref(0)
const anchorId = ref<number | null>(null)
const filters: { key: Filter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '需要确认' },
  { key: 'failed', label: '失败' },
  { key: 'cancelled', label: '已取消' },
]

const proposalTitleByAction = computed(() => {
  const result = new Map<number, string>()
  for (const event of auditEvents.value) {
    if (event.action_id !== null && event.event_type === 'proposal_created') {
      const title = titleIn(event.details, 'before')
      if (title) result.set(event.action_id, title)
    }
  }
  return result
})
const weekStart = computed(() => {
  const today = localParts(new Date(state.clockNow).toISOString())!.date
  const date = new Date(`${today}T12:00:00+08:00`)
  const weekday = (date.getUTCDay() + 6) % 7
  date.setUTCDate(date.getUTCDate() - weekday)
  return localParts(date.toISOString())!.date
})
const since = computed(() => thisWeek.value ? Math.floor(new Date(`${weekStart.value}T00:00:00+08:00`).getTime() / 1000) : undefined)
const pendingActions = computed(() => new Set(state.actions.filter(action =>
  action.status === 'pending' && new Date(action.expires_at).getTime() > state.clockNow,
).map(action => action.id)))
const pendingEvents = computed<AuditEvent[]>(() => state.actions.filter(action =>
  pendingActions.value.has(action.id) && (since.value === undefined || new Date(action.created_at).getTime() / 1000 >= since.value),
).map(action => ({
  id: -action.id, event_type: 'proposal_created', task_id: action.task_id,
  action_id: action.id, request_id: action.request_id || null,
  conversation_id: null, tool_call_id: null, from_status: null, to_status: 'pending',
  reason: null, details: null, created_at: action.created_at,
  subject_title: action.before.title,
})))
const pageCount = computed(() => filter.value === 'pending' ? Math.ceil(pendingEvents.value.length / pageSize) : totalPages.value)
const recordCount = computed(() => filter.value === 'pending' ? pendingEvents.value.length : totalRecords.value)
const events = computed(() => filter.value === 'pending'
  ? pendingEvents.value.slice((currentPage.value - 1) * pageSize, currentPage.value * pageSize)
  : auditEvents.value)
const pageNumbers = computed(() => {
  const count = pageCount.value
  if (!count) return []
  const start = Math.max(1, Math.min(currentPage.value - 2, count - 4))
  return Array.from({ length: Math.min(5, count - start + 1) }, (_, index) => start + index)
})

let pageRequest = 0
let summaryRequest = 0
async function loadPage(requestedPage = 1, resetSnapshot = false) {
  const current = ++pageRequest
  lastRequestedPage.value = requestedPage
  pageError.value = ''
  expandedId.value = null
  related.value = []
  if (resetSnapshot) anchorId.value = null
  if (filter.value === 'pending') {
    currentPage.value = Math.min(requestedPage, Math.max(1, pageCount.value))
    jumpPage.value = currentPage.value
    auditEvents.value = []
    pageLoading.value = false
    return
  }
  auditEvents.value = []
  totalPages.value = 0
  totalRecords.value = 0
  pageLoading.value = true
  try {
    const page = await api.eventPage(
      requestedPage, pageSize, since.value,
      filter.value === 'failed' || filter.value === 'cancelled' ? filter.value : 'all',
      anchorId.value ?? undefined,
    )
    if (current !== pageRequest) return
    auditEvents.value = page.items
    currentPage.value = page.page
    jumpPage.value = page.page
    totalPages.value = page.total_pages
    totalRecords.value = page.total
    anchorId.value = page.anchor_id
  } catch (error) {
    if (current === pageRequest) pageError.value = error instanceof Error ? error.message : '加载记录失败'
  } finally { if (current === pageRequest) pageLoading.value = false }
}
function goToPage(value: number) {
  if (!Number.isInteger(value) || value < 1 || value > pageCount.value) {
    pageError.value = `请输入 1 到 ${pageCount.value} 之间的页数。`
    return
  }
  void loadPage(value)
}
function retryPage() {
  void loadPage(lastRequestedPage.value)
  void loadSummary()
}
async function loadSummary() {
  const current = ++summaryRequest
  summary.value = null
  try {
    const result = await api.auditSummary(since.value)
    if (current === summaryRequest) summary.value = result
  } catch (error) {
    if (current === summaryRequest) pageError.value = error instanceof Error ? error.message : '加载统计失败'
  }
}
watch([filter, thisWeek, weekStart], () => { void loadPage(1, true) }, { immediate: true })
watch([thisWeek, weekStart], () => { void loadSummary() }, { immediate: true })
watch(() => state.events[0]?.id, (id, previous) => {
  if (id !== previous) {
    void loadPage(currentPage.value, true)
    void loadSummary()
  }
})
watch(pageCount, count => {
  if (filter.value === 'pending' && currentPage.value > Math.max(1, count)) {
    currentPage.value = Math.max(1, count)
    jumpPage.value = currentPage.value
  }
})

function titleIn(details: Record<string, unknown> | null, key: string): string | null {
  const value = details?.[key]
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const title = (value as Record<string, unknown>).title
  return typeof title === 'string' ? title : null
}
function taskTitle(event: AuditEvent) {
  return event.subject_title || titleIn(event.details, 'task') || titleIn(event.details, 'before') || titleIn(event.details, 'after')
    || (event.action_id !== null ? proposalTitleByAction.value.get(event.action_id) : null)
    || state.tasks.find(task => task.id === event.task_id)?.title || `待办 #${event.task_id}`
}
function description(event: AuditEvent) {
  const title = taskTitle(event)
  const labels: Record<string, string> = {
    task_created: `创建待办：${title}`,
    task_updated: `修改待办：${title}`,
    task_deleted: `删除待办：${title}`,
    proposal_created: `提出${event.details?.action_type === 'delete' ? '删除' : '修改'}：${title}`,
    proposal_replaced: `替换提案：${title}`,
    proposal_expired: `提案过期：${title}`,
    proposal_executed: `确认执行：${title}`,
    proposal_cancelled: `取消提案：${title}`,
    proposal_conflicted: `执行冲突：${title}`,
    proposal_failed: `执行失败：${title}`,
    proposal_deleted: `清理过期提案：${title}`,
  }
  return labels[event.event_type] || `记录操作：${title}`
}
function actor(event: AuditEvent) {
  if (event.event_type === 'proposal_expired' || event.event_type === 'proposal_deleted') return '系统'
  if (event.event_type === 'proposal_replaced' && event.details?.manual_operation) return '你'
  if (event.event_type === 'proposal_created' || event.event_type === 'proposal_replaced') return '小记'
  if (event.event_type === 'task_created') return event.conversation_id ? '小记' : '你'
  if (event.event_type === 'proposal_executed' || event.event_type === 'proposal_cancelled' || event.event_type === 'proposal_conflicted' || event.event_type === 'proposal_failed') return '你'
  return event.details?.source === 'manual' ? '你' : event.conversation_id ? '小记' : '你'
}
function status(event: AuditEvent) {
  if (event.event_type === 'proposal_created') {
    if (event.action_id !== null && pendingActions.value.has(event.action_id)) return { label: '待确认', className: '' }
    return { label: '已提出', className: 'muted' }
  }
  if (event.event_type === 'proposal_failed' || event.event_type === 'proposal_conflicted') return { label: '失败', className: 'error' }
  if (event.event_type === 'proposal_cancelled' || event.event_type === 'proposal_expired' || event.event_type === 'proposal_replaced' || event.event_type === 'proposal_deleted') return { label: { proposal_cancelled: '已取消', proposal_expired: '已过期', proposal_replaced: '已替换', proposal_deleted: '已清理' }[event.event_type] || '已结束', className: 'muted' }
  return { label: event.event_type === 'task_created' ? '已创建' : '已执行', className: 'ok' }
}
function eventTime(value: string) {
  const date = new Date(value)
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date)
}
function relatedEvents(event: AuditEvent) {
  return related.value.filter(other => other.id !== event.id && (
    event.action_id !== null && other.action_id === event.action_id
    || event.request_id !== null && other.request_id === event.request_id
  )).sort((a, b) => a.id - b.id)
}
async function openRow(event: AuditEvent) {
  if (expandedId.value === event.id) { expandedId.value = null; return }
  expandedId.value = event.id
  related.value = []
  if (event.action_id === null && event.request_id === null) return
  try {
    const rows = await api.events({
      limit: 100,
      actionId: event.action_id ?? undefined,
      requestId: event.action_id === null ? event.request_id ?? undefined : undefined,
    })
    if (expandedId.value === event.id) related.value = rows
  } catch (error) { if (expandedId.value === event.id) pageError.value = error instanceof Error ? error.message : '加载相关记录失败' }
}
function pendingAction(event: AuditEvent): Action | undefined {
  if (event.event_type !== 'proposal_created' || event.action_id === null || !pendingActions.value.has(event.action_id)) return undefined
  return state.actions.find(action => action.id === event.action_id)
}
function proposalChangesFor(event: AuditEvent) {
  const action = pendingAction(event)
  return action ? proposalChanges(action) : []
}
async function decide(event: AuditEvent, decision: 'confirm' | 'cancel') {
  const action = pendingAction(event)
  if (!action || state.busy) return
  state.busy = true
  state.error = ''
  try {
    const result = await (decision === 'confirm' ? api.confirm(action.id) : api.cancel(action.id))
    state.toast = result.status === 'executed' ? '提案已执行，任务已更新。' : result.status === 'cancelled' ? '提案已取消。' : `提案状态：${result.status}`
    await refresh()
  } catch (error) { state.error = error instanceof Error ? error.message : '操作失败' }
  finally { state.busy = false }
}
</script>

<template>
  <main class="audit-page page">
    <div class="audit-heading"><h1>留痕</h1><p>每一次任务操作和提案流转，都有记录可查。</p></div>
    <div class="audit-metrics" aria-label="操作概览">
      <div><strong>{{ summary?.total ?? '—' }}</strong><span>操作记录</span></div>
      <div><strong>{{ summary?.proposals ?? '—' }}</strong><span>提案 · 确认 {{ summary?.executed ?? '—' }} / 取消 {{ summary?.cancelled ?? '—' }} / 过期 {{ summary?.expired ?? '—' }}</span></div>
      <div><strong>{{ summary?.failures ?? '—' }}</strong><span>失败与冲突</span></div>
      <div><strong>{{ summary?.requests ?? '—' }}</strong><span>关联请求</span></div>
    </div>
    <div class="audit-filters" aria-label="筛选操作记录">
      <button v-for="item in filters" :key="item.key" type="button" :class="{ active: filter === item.key }" :aria-pressed="filter === item.key" @click="filter = item.key">{{ item.label }}</button>
      <span class="audit-filter-spacer" />
      <button type="button" :class="{ active: thisWeek }" :aria-pressed="thisWeek" @click="thisWeek = !thisWeek">{{ thisWeek ? '本周 ✓' : '本周' }}</button>
    </div>
    <div v-if="pageError" class="notice error" role="alert">{{ pageError }}<span><button type="button" @click="retryPage">重试</button><button type="button" aria-label="关闭提示" @click="pageError = ''">×</button></span></div>
    <div class="audit-list">
      <div v-for="event in events" :key="event.id" class="audit-entry">
        <div class="audit-row" :class="{ expanded: expandedId === event.id }">
          <time :datetime="event.created_at">{{ eventTime(event.created_at) }}</time>
          <span class="audit-actor" :class="{ ai: actor(event) === '小记' }">{{ actor(event) }}</span>
          <span class="audit-description">{{ description(event) }}</span>
          <span class="audit-status" :class="status(event).className">{{ status(event).label }}</span>
          <button type="button" class="audit-trace" :aria-expanded="expandedId === event.id" @click="openRow(event)">{{ expandedId === event.id ? '收起' : pendingAction(event) ? '处理' : '轨迹' }}</button>
        </div>
        <div v-if="expandedId === event.id" class="audit-detail">
          <div v-if="pendingAction(event)" class="audit-decision">
            <div class="audit-decision-head"><strong>{{ pendingAction(event)?.action_type === 'delete' ? '删除待办' : '修改待办' }} · {{ pendingAction(event)?.before.title }}</strong><span>{{ eventTime(pendingAction(event)!.expires_at) }} 前有效</span></div>
            <p>{{ pendingAction(event)?.action_type === 'delete' ? '确认后才会删除这条待办。' : '确认后才会写入以下修改。' }}</p>
            <div v-for="change in proposalChangesFor(event)" :key="change.label" class="audit-change"><span>{{ change.label }}</span><div class="diff-columns"><div class="diff-col"><span class="tiny">修改前</span><b>{{ change.before }}</b></div><span class="diff-arrow">→</span><div class="diff-col new"><span class="tiny">修改后</span><b>{{ change.after }}</b></div></div></div>
            <div class="diff-actions"><button class="solid" type="button" :disabled="state.busy" @click="decide(event, 'confirm')">{{ pendingAction(event)?.action_type === 'delete' ? '确认删除' : '确认修改' }}</button><button class="outline" type="button" :disabled="state.busy" @click="decide(event, 'cancel')">保持原样</button></div>
          </div>
          <div class="audit-trace-meta"><span v-if="event.id > 0">记录 #{{ event.id }}</span><span>待办 #{{ event.task_id }}</span><span v-if="event.action_id !== null">提案 #{{ event.action_id }}</span></div>
          <p v-if="event.reason" class="audit-reason">{{ event.reason }}</p>
          <div class="audit-trace-lines">
            <div v-if="event.request_id"><b>请求 ID</b><span>{{ event.request_id }}</span></div>
            <div v-if="event.conversation_id"><b>对话 ID</b><span>{{ event.conversation_id }}</span></div>
            <div v-if="event.tool_call_id"><b>工具调用 ID</b><span>{{ event.tool_call_id }}</span></div>
            <div v-if="event.from_status || event.to_status"><b>状态变化</b><span>{{ event.from_status || '—' }} → {{ event.to_status || '—' }}</span></div>
          </div>
          <details v-if="event.details && Object.keys(event.details).length"><summary>查看记录数据</summary><pre>{{ JSON.stringify(event.details, null, 2) }}</pre></details>
          <div v-if="relatedEvents(event).length" class="audit-related"><b>相关记录</b><div v-for="other in relatedEvents(event)" :key="other.id">{{ eventTime(other.created_at) }} · {{ description(other) }}</div></div>
        </div>
      </div>
      <div v-if="!events.length" class="audit-empty">{{ pageLoading || filter === 'pending' && state.loading ? '正在加载记录…' : pageError ? '加载失败，请重试。' : '当前筛选下没有操作记录。' }}</div>
      <div v-if="pageCount" class="audit-pagination" aria-label="操作记录分页">
        <span class="audit-page-total">共 {{ recordCount }} 条 · 第 {{ currentPage }} / {{ pageCount }} 页</span>
        <button type="button" class="outline" :disabled="pageLoading || currentPage <= 1" @click="goToPage(currentPage - 1)">上一页</button>
        <button v-for="number in pageNumbers" :key="number" type="button" class="audit-page-number" :class="{ active: currentPage === number }" :aria-label="`第 ${number} 页`" :aria-current="currentPage === number ? 'page' : undefined" :disabled="pageLoading" @click="goToPage(number)">{{ number }}</button>
        <button type="button" class="outline" :disabled="pageLoading || currentPage >= pageCount" @click="goToPage(currentPage + 1)">下一页</button>
        <form class="audit-page-jump" @submit.prevent="goToPage(jumpPage)"><label for="audit-jump-page">跳至</label><input id="audit-jump-page" v-model.number="jumpPage" type="number" min="1" :max="pageCount" step="1" required aria-label="跳转页数" /><span>页</span><button type="submit" class="outline" :disabled="pageLoading">跳转</button></form>
      </div>
    </div>
  </main>
</template>
