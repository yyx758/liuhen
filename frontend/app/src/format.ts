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

/** 账号相关时间：“3 分钟前”这类相对时间，超过 7 天显示日期。 */
export function ago(value: string | null, now = Date.now()) {
  if (!value) return '从未'
  const diff = now - new Date(value).getTime()
  const hour = 3600e3, day = 24 * hour
  if (diff < hour) return `${Math.max(1, Math.round(diff / 60e3))} 分钟前`
  if (diff < day) return `${Math.round(diff / hour)} 小时前`
  if (diff < 7 * day) return `${Math.round(diff / day)} 天前`
  const local = localParts(value)!
  return `${Number(local.date.slice(5, 7))}月${Number(local.date.slice(8))}日`
}

/** 精确到分钟的时间，如 “11-21 15:30”。 */
export function stamp(value: string) {
  const local = localParts(value)!
  return `${local.date.slice(5)} ${local.time}`
}

/** 把 User-Agent 粗略解析为“浏览器 / 系统”。 */
export function deviceLabel(userAgent: string | null) {
  if (!userAgent) return '未知设备'
  const browsers: [RegExp, string][] = [[/Edg\//, 'Edge'], [/OPR\//, 'Opera'], [/Firefox\//, 'Firefox'], [/MicroMessenger/, '微信'], [/Chrome\//, 'Chrome'], [/Safari\//, 'Safari']]
  const systems: [RegExp, string][] = [[/Windows/, 'Windows'], [/iPhone|iPad/, 'iOS'], [/Android/, 'Android'], [/Mac OS X/, 'macOS'], [/Linux/, 'Linux']]
  const browser = browsers.find(([pattern]) => pattern.test(userAgent))?.[1] ?? '其他浏览器'
  const system = systems.find(([pattern]) => pattern.test(userAgent))?.[1] ?? '其他系统'
  return `${browser} / ${system}`
}

export const ACCOUNT_EVENT_LABELS: Record<string, string> = {
  user_registered: '提交注册', user_approved: '审核通过', user_rejected: '拒绝注册', user_disabled: '停用账号',
  user_enabled: '启用账号', user_deleted: '删除账号', password_reset: '重置密码', password_changed: '修改密码',
  email_changed: '修改邮箱', role_changed: '角色变更', chat_limit_changed: '调整对话上限',
  login_succeeded: '登录成功', login_failed: '登录失败', login_locked: '登录锁定', session_revoked: '下线设备',
}
