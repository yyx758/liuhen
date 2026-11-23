<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { Task } from '../api'
import { localParts, sortTasks, taskDate, taskTime, today } from '../format'
import { sendChat, state } from '../state'
import TaskRow from '../components/TaskRow.vue'
import Icon from '../components/Icon.vue'

const router = useRouter()
const command = ref('')
const todayTasks = computed(() => sortTasks(state.tasks.filter(task => taskDate(task) === today())))
const todo = computed(() => todayTasks.value.filter(task => task.status === 'todo'))
const allTodo = computed(() => state.tasks.filter(task => task.status === 'todo').length)
const upcoming = computed(() => sortTasks(state.tasks.filter(task => taskDate(task) > today() && task.status === 'todo')).slice(0, 4))
const dateLabel = computed(() => {
  const local = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric', weekday: 'short', hour: 'numeric', hourCycle: 'h23' }).formatToParts(new Date())
  const part = (type: string) => local.find(item => item.type === type)?.value || ''
  const hour = Number(part('hour'))
  return `${part('month')}月${part('day')}日，${part('weekday')} · ${hour < 11 ? '早上好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好'}`
})
const nowHour = new Date().getHours() + new Date().getMinutes() / 60
const nowLeft = `${(nowHour - 8) / 14 * 100}%`

function isAiTask(task: Task) {
  return Boolean(task.created_by_agent)
}
// 时间轴：今天开始的任务放在开始时间；今天截止（且不是今天开始）的任务放在截止时间并标注"截止"。
type TimelineSlot = { task: Task; time: string; due: boolean }
const timeline = computed<TimelineSlot[]>(() => state.tasks.filter(task => task.status === 'todo').flatMap(task => {
  const start = localParts(task.start_at)
  if (start?.date === today()) return [{ task, time: start.time, due: false }]
  const due = localParts(task.due_at)
  return due?.date === today() ? [{ task, time: due.time, due: true }] : []
}).sort((a, b) => a.time.localeCompare(b.time)))

function slotStyle(slot: TimelineSlot) {
  const hour = Number(slot.time.slice(0, 2)) + Number(slot.time.slice(3, 5)) / 60
  const width = slot.due ? 8 : Math.min(Math.max((slot.task.duration_minutes || 0) / 60 / 14 * 100, 8), 22)
  // 时间轴只画 08:00–22:00；范围外的（如 23:59 截止）贴在两端，不能被挤出可视区域。
  const left = Math.max((hour - 8) / 14 * 100, 0)
  return { left: `min(${left}%, calc(100% - max(${width}%, 80px)))`, width: `${width}%` }
}
function eventLabel(eventType: string) {
  const labels: Record<string, string> = {
    task_created: '添加了一项待办', task_updated: '修改了一项待办', task_deleted: '删除了一项待办',
    proposal_created: '准备了一项修改，等待确认',
    proposal_executed: '执行了一项已确认的修改', proposal_cancelled: '取消了一项修改',
    proposal_expired: '一项修改建议已过期',
  }
  return labels[eventType] || '记录了一次操作'
}
async function submit() {
  const text = command.value.trim()
  if (!text || state.busy) return
  command.value = ''
  await router.push('/chat')
  await sendChat(text)
}
</script>

<template>
  <main class="page">
    <section class="home-hero">
      <div class="date-label">{{ dateLabel }}</div>
      <h1 class="hero-title">想让<span class="ink-line">小记</span>做点什么？</h1>
      <form id="homeCommand" class="command-form" @submit.prevent="submit">
        <input v-model="command" autocomplete="off" aria-label="告诉小记你的安排" placeholder="比如：明天下午三点提醒我复习 Redis" required />
        <button type="submit" class="command-send" :disabled="state.busy" aria-label="发送给小记"><Icon name="arrow" :size="21" /></button>
      </form>
      <div class="examples">
        <span class="tiny">试着说</span>
        <button v-for="sample in ['明天下午三点提醒我复习 Redis', '后天上午十点安排项目复盘']" :key="sample" type="button" class="sample" @click="command = sample">{{ sample }}</button>
      </div>
      <div class="home-metrics">
        <div class="metric"><strong>{{ String(todayTasks.length).padStart(2, '0') }}</strong>今日安排</div>
        <div class="metric"><strong>{{ String(todo.length).padStart(2, '0') }}</strong>今日待完成</div>
        <div class="metric"><strong>{{ String(allTodo).padStart(2, '0') }}</strong>全部待办</div>
      </div>
    </section>

    <div class="main-grid">
      <div>
        <section>
          <div class="section-heading"><h2 class="section-title">今天的时间轴</h2><RouterLink to="/calendar" class="subtle-link">查看日历 <Icon name="arrow" :size="14" /></RouterLink></div>
          <div class="timeline-wrap"><div class="timeline">
            <div class="timeline-hours"><span v-for="hour in 14" :key="hour">{{ String(hour + 7).padStart(2, '0') }}</span></div>
            <div v-if="nowHour >= 8 && nowHour <= 22" class="timeline-now" :style="{ left: nowLeft }"><span>现在</span></div>
            <button v-for="slot in timeline" :key="slot.task.id" type="button" class="slot" :class="{ ai: isAiTask(slot.task), important: !isAiTask(slot.task) && slot.task.priority === 'high' }" :style="slotStyle(slot)" @click="state.detailTaskId = slot.task.id"><strong>{{ slot.due ? `${slot.time} 截止` : slot.time }}</strong>{{ slot.task.title }}</button>
            <div v-if="!timeline.length" class="timeline-empty">今天的安排还很轻松。</div>
          </div></div>
          <div class="timeline-footnote"><span class="highlight">黄色</span> 是小记帮你安排的时间，可以随时调整。</div>
        </section>
        <section class="section-gap">
          <div class="section-heading"><h2 class="section-title">今天还没做</h2><RouterLink to="/tasks" class="subtle-link">全部待办 <Icon name="arrow" :size="14" /></RouterLink></div>
          <div class="task-list"><TaskRow v-for="task in todo" :key="task.id" :task="task" /><div v-if="!todo.length" class="empty-state" style="padding:38px"><h3>今天的待办已经清空</h3><p>可以给自己留一点休息时间。</p></div></div>
        </section>
      </div>
      <aside>
        <div class="info-panel">
          <div class="section-heading" style="margin-bottom:6px"><h3 class="mini-heading" style="margin:0">即将到来</h3><span class="tag blue">未来几天</span></div>
          <div v-for="task in upcoming" :key="task.id" class="upcoming-row"><div class="date-block"><strong>{{ Number(taskDate(task).slice(-2)) }}</strong><span>{{ Number(taskDate(task).slice(5, 7)) }}月</span></div><div class="upcoming-txt"><b>{{ task.title }}</b><small>{{ taskTime(task) }} · 其他</small></div><button class="text-btn" type="button" aria-label="打开详情" @click="state.detailTaskId = task.id"><Icon name="chevron-right" :size="16" /></button></div>
          <p v-if="!upcoming.length" class="small" style="padding:12px 0">近期没有额外安排。</p>
          <RouterLink to="/calendar" class="subtle-link" style="margin-top:7px">去日历查看 <Icon name="arrow" :size="13" /></RouterLink>
        </div>
        <div class="activity">
          <div class="section-heading"><h3 class="mini-heading" style="margin:0">最近动态</h3><RouterLink to="/history?filter=pending" class="subtle-link">审核提案</RouterLink></div>
          <div v-for="event in state.events.slice(0, 3)" :key="event.id" class="activity-row"><span class="mini-ai">{{ event.details?.source === 'manual' || event.event_type === 'task_created' && !event.conversation_id ? '你' : '小记' }}</span><b>{{ eventLabel(event.event_type) }}</b></div>
          <div v-if="!state.events.length" class="activity-row">还没有新的动态。</div>
        </div>
      </aside>
    </div>
  </main>
</template>
