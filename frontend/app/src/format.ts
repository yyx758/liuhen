import type { Action, Task } from './api'

const zone = 'Asia/Taipei'
const parts = new Intl.DateTimeFormat('en-CA', {
  timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export function localParts(value: string | null) {
  if (!value) return null
  const data = Object.fromEntries(parts.formatToParts(new Date(value)).map(part => [part.type, part.value]))
  return { date: `${data.year}-${data.month}-${data.day}`, time: `${data.hour}:${data.minute}` }
}

export function taskDate(task: Task) { return localParts(task.start_at || task.due_at)?.date || '' }
export function taskTime(task: Task) { return localParts(task.start_at || task.due_at)?.time || '全天' }
export function today() { return localParts(new Date().toISOString())!.date }
export function displayDate(value: string | null) {
  const local = localParts(value)
  if (!local) return '未安排'
  const date = new Date(`${local.date}T12:00:00`)
  return `${date.getMonth() + 1}月${date.getDate()}日 · 周${'日一二三四五六'[date.getDay()]} ${local.time}`
}
export function toApiDateTime(value: string) { return value ? `${value}:00+08:00` : null }
export function sortTasks(tasks: Task[]) {
  return [...tasks].sort((a, b) => (a.start_at || a.due_at || '').localeCompare(b.start_at || b.due_at || ''))
}

export function proposalChanges(action: Action) {
  if (!action.after) return [{ label: '待办', before: action.before.title, after: '删除这条待办' }]
  const fields: { key: keyof Task; label: string; format: (value: Task[keyof Task]) => string }[] = [
    { key: 'title', label: '标题', format: value => String(value) },
    { key: 'priority', label: '优先级', format: value => ({ low: '低', medium: '中', high: '高' })[String(value) as Task['priority']] },
    { key: 'start_at', label: '开始时间', format: value => value ? displayDate(String(value)) : '未设置' },
    { key: 'duration_minutes', label: '预计用时', format: value => value === null ? '未设置' : `${value} 分钟` },
    { key: 'due_at', label: '截止时间', format: value => value ? displayDate(String(value)) : '未设置' },
    { key: 'status', label: '状态', format: value => value === 'done' ? '已完成' : '未完成' },
  ]
  return fields.filter(({ key }) => action.before[key] !== action.after?.[key]).map(({ key, label, format }) => ({
    label, before: format(action.before[key]), after: format(action.after![key]),
  }))
}
