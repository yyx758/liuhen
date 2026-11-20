<script setup lang="ts">
import { computed, ref } from 'vue'
import { sortTasks, taskDate, taskTime, today } from '../format'
import { state } from '../state'
import Icon from '../components/Icon.vue'

const selected = ref(today())
const cursor = ref(new Date(`${today()}T12:00:00`))
const mode = ref<'month' | 'week'>('month')
const monthTitle = computed(() => mode.value === 'month'
  ? `${cursor.value.getFullYear()} 年 ${cursor.value.getMonth() + 1} 月`
  : `${displayDay(days.value[0]!.key)} — ${displayDay(days.value[6]!.key)}`)
const selectedTasks = computed(() => sortTasks(state.tasks.filter(task => taskDate(task) === selected.value)))
const completedToday = computed(() => selectedTasks.value.filter(task => task.status === 'done').length)

function localDate(date: Date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` }
function displayDay(value: string) { const date = new Date(`${value}T12:00:00`); return `${date.getMonth() + 1}月${date.getDate()}日 周${'日一二三四五六'[date.getDay()]}` }
const days = computed(() => {
  const anchor = mode.value === 'month' ? new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1, 12) : new Date(`${selected.value}T12:00:00`)
  const mondayOffset = (anchor.getDay() + 6) % 7
  anchor.setDate(anchor.getDate() - mondayOffset)
  const count = mode.value === 'week' ? 7 : Math.ceil((mondayOffset + new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 0).getDate()) / 7) * 7
  return Array.from({ length: count }, (_, index) => {
    const day = new Date(anchor)
    day.setDate(anchor.getDate() + index)
    const key = localDate(day)
    return { key, number: day.getDate(), dim: day.getMonth() !== cursor.value.getMonth(), tasks: sortTasks(state.tasks.filter(task => taskDate(task) === key)) }
  })
})
function move(offset: number) {
  const next = new Date(cursor.value)
  if (mode.value === 'month') { next.setDate(1); next.setMonth(next.getMonth() + offset) }
  else next.setDate(next.getDate() + offset * 7)
  cursor.value = next
  selected.value = localDate(next)
}
function select(date: string) { selected.value = date; cursor.value = new Date(`${date}T12:00:00`) }
function reset() { selected.value = today(); cursor.value = new Date(`${today()}T12:00:00`) }
</script>

<template>
  <main class="page"><div class="page-head"><div><div class="page-kicker"><span class="dot" /> CALENDAR</div><h1 class="page-title">日历</h1><p class="lead">让安排变得清晰，也为临时的灵感留一点空间。</p></div><div class="head-actions"><button class="outline" type="button" @click="reset">回到今天</button><button class="solid" type="button" @click="state.editorTaskId = null; state.editorOpen = true"><Icon name="plus" :size="15" />添加日程</button></div></div>
    <div class="calendar-toolbar"><div class="month-name">{{ monthTitle }}</div><div class="calendar-controls"><button class="square-btn" type="button" aria-label="上一页" @click="move(-1)"><Icon name="chevron-left" :size="17" /></button><button class="square-btn" type="button" aria-label="下一页" @click="move(1)"><Icon name="chevron-right" :size="17" /></button><div class="segmented"><button type="button" :class="{ on: mode === 'month' }" @click="mode = 'month'">月视图</button><button type="button" :class="{ on: mode === 'week' }" @click="mode = 'week'">周视图</button></div></div></div>
    <div class="calendar-layout"><section class="calendar-board"><div class="weekdays"><div v-for="day in ['周一', '周二', '周三', '周四', '周五', '周六', '周日']" :key="day">{{ day }}</div></div><div class="days-grid"><div v-for="day in days" :key="day.key" class="day-cell" :class="{ dim: day.dim && mode === 'month', active: day.key === selected, today: day.key === today() }" role="button" tabindex="0" :aria-label="displayDay(day.key)" @click="select(day.key)" @keydown.enter="select(day.key)" @keydown.space.prevent="select(day.key)"><div class="day-number">{{ day.number }}</div><button v-for="task in day.tasks.slice(0, 3)" :key="task.id" type="button" class="cal-chip other" :title="task.title" @click.stop="state.detailTaskId = task.id">{{ task.start_at ? `${taskTime(task)} ` : '' }}{{ task.title }}</button><span v-if="day.tasks.length > 3" class="cal-more">还有 {{ day.tasks.length - 3 }} 项</span></div></div></section>
      <aside class="selected-day"><div class="page-kicker"><span class="dot" /> SELECTED DAY</div><h3>{{ displayDay(selected) }}</h3><div class="selected-week">{{ selectedTasks.length }} 项安排 · {{ completedToday }} 项已完成</div><button v-for="task in selectedTasks" :key="task.id" class="selected-task" type="button" @click="state.detailTaskId = task.id"><span class="time">{{ taskTime(task) }}</span><span class="n">{{ task.title }}<small>其他 {{ task.status === 'done' ? '· 已完成' : '' }}</small></span></button><div v-if="!selectedTasks.length" class="selected-empty">今天还没有安排。<br />可以放心地享受一点空白。</div><button type="button" class="outline" style="width:100%;margin-top:13px" @click="state.editorTaskId = null; state.editorOpen = true"><Icon name="plus" :size="16" />给这天加个安排</button><div class="calendar-tip"><span class="tag ai">小记建议</span><div style="margin-top:9px">安排太满的时候，可以在对话中请小记帮你调整。调整前会展示修改内容，交由你确认。</div></div></aside></div>
  </main>
</template>
