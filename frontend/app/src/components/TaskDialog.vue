<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { api, type TaskInput, type TaskUpdate } from '../api'
import { REMINDER_OPTIONS, localParts, reminderLabel, toApiDateTime, today } from '../format'
import { runAction, state } from '../state'
import Icon from './Icon.vue'

const editing = computed(() => state.tasks.find(task => task.id === state.editorTaskId) || null)
// remind：'' 表示不提醒，其余为提前分钟数的字符串（select 的值只能是字符串）。
const form = reactive({ date: today(), time: '14:00', priority: 'medium' as TaskInput['priority'], title: '', duration: '45', remind: '' })
// Agent 可能设置了预设以外的值（如提前 2 小时），编辑时也要能选中它。
const reminderOptions = computed(() => {
  const current = editing.value?.remind_before_minutes ?? null
  return REMINDER_OPTIONS.includes(current) ? REMINDER_OPTIONS : [...REMINDER_OPTIONS, current]
})
const reminderPassed = computed(() => {
  if (form.remind === '') return false
  const dateTime = toApiDateTime(`${form.date}T${form.time}`)
  return Boolean(dateTime) && new Date(dateTime!).getTime() <= Date.now()
})
let createPayload = ''
let createKey = ''

watch(() => [state.editorOpen, state.editorTaskId] as const, () => {
  createPayload = ''
  createKey = ''
  const task = editing.value
  const local = localParts(task?.start_at || task?.due_at || null)
  form.title = task?.title || ''
  form.date = local?.date || today()
  form.time = local?.time || '14:00'
  form.priority = task?.priority || 'medium'
  form.duration = String(task?.duration_minutes || 45)
  form.remind = task?.remind_before_minutes == null ? '' : String(task.remind_before_minutes)
})

async function submit() {
  const task = editing.value
  const dateTime = toApiDateTime(`${form.date}T${form.time}`)
  if (!dateTime) { state.error = '请选择日期和开始时间。'; return }
  const duration = Number(form.duration)
  if (!Number.isInteger(duration) || duration < 5 || duration > 720) { state.error = '预计用时应为 5 到 720 分钟。'; return }
  const remind = form.remind === '' ? null : Number(form.remind)
  const input: TaskInput = {
    title: form.title.trim(), priority: form.priority, remind_before_minutes: remind,
    start_at: task?.start_at || !task?.due_at ? dateTime : null,
    due_at: task?.start_at ? task.due_at : task?.due_at ? dateTime : null,
    duration_minutes: task?.start_at || !task?.due_at ? duration : null,
  }
  const patch: TaskUpdate = { title: input.title, priority: input.priority, remind_before_minutes: remind }
  if (task?.start_at) { patch.start_at = dateTime; patch.due_at = task.due_at }
  else if (task?.due_at) patch.due_at = dateTime
  if (task?.start_at) patch.duration_minutes = duration
  const payload = JSON.stringify(input)
  if (payload !== createPayload) { createPayload = payload; createKey = crypto.randomUUID() }
  const ok = task
    ? await runAction(() => api.updateTask(task.id, task.version, patch), '待办已修改。')
    : await runAction(() => api.createTask(input, createKey), '待办已创建。')
  if (ok) state.editorOpen = false
}
</script>

<template>
  <div v-if="state.editorOpen" class="overlay" @click.self="state.editorOpen = false">
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><h2 id="modal-title">{{ editing ? '编辑待办' : '记一笔' }}</h2><p>把想做的事写下来，剩下的慢慢安排。</p></div><button class="close-btn" type="button" aria-label="关闭" @click="state.editorOpen = false"><Icon name="x" :size="20" /></button></div>
      <form @submit.prevent="submit">
        <div class="form-group"><label class="form-label" for="task-title">待办名称 *</label><input id="task-title" v-model="form.title" class="field" type="text" maxlength="80" required autofocus placeholder="例如：完成项目复盘" /></div>
        <div class="field-row"><div class="form-group"><label class="form-label" for="task-date">日期 *</label><input id="task-date" v-model="form.date" class="field" type="date" required /></div><div class="form-group"><label class="form-label" for="task-time">开始时间</label><input id="task-time" v-model="form.time" class="field" type="time" /></div></div>
        <div class="field-row"><div class="form-group"><label class="form-label" for="task-category">分类</label><select id="task-category" class="field" disabled title="分类服务尚未接入"><option>其他</option></select></div><div class="form-group"><label class="form-label" for="task-priority">优先级</label><select id="task-priority" v-model="form.priority" class="field"><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></div></div>
        <div class="field-row"><div class="form-group"><label class="form-label" for="task-duration">预计用时（分钟）</label><input id="task-duration" v-model="form.duration" class="field" type="number" min="5" max="720" step="5" /></div><div class="form-group"><label class="form-label" for="task-remind">提醒</label><select id="task-remind" v-model="form.remind" class="field"><option v-for="value in reminderOptions" :key="String(value)" :value="value === null ? '' : String(value)">{{ reminderLabel(value) }}</option></select><span v-if="reminderPassed" class="tiny">任务时间已过，不会提醒</span></div></div>
        <div class="form-group"><label class="form-label" for="task-note">备注</label><textarea id="task-note" class="field fieldarea" placeholder="写点备注，方便以后想起来…" readonly title="备注服务尚未接入" /></div>
        <div class="modal-actions"><button class="outline" type="button" @click="state.editorOpen = false">取消</button><button class="solid" type="submit" :disabled="state.busy">{{ state.busy ? '提交中…' : editing ? '保存修改' : '添加待办' }}</button></div>
      </form>
    </section>
  </div>
</template>
