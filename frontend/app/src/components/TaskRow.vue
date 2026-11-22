<script setup lang="ts">
import type { Task } from '../api'
import { state, runAction } from '../state'
import { api } from '../api'
import { displayDate, reminderLabel, taskTime, willRemind } from '../format'
import Icon from './Icon.vue'

defineProps<{ task: Task; detailed?: boolean }>()

function isAiTask(task: Task) {
  return Boolean(task.created_by_agent)
}

function toggle(task: Task) {
  void runAction(() => api.updateTask(task.id, task.version, { status: task.status === 'done' ? 'todo' : 'done' }), '任务状态已更新。')
}
</script>

<template>
  <div class="task-item" :class="{ done: task.status === 'done' }">
    <button class="task-check" :class="{ done: task.status === 'done' }" type="button" :disabled="state.busy" :aria-label="task.status === 'done' ? '标记未完成' : '标记完成'" @click="toggle(task)">
      <Icon v-if="task.status === 'done'" name="check" :size="14" />
    </button>
    <button class="task-main" type="button" @click="state.detailTaskId = task.id">
      <span class="task-name">{{ task.title }}</span>
      <span v-if="detailed" class="task-desc">{{ task.start_at ? displayDate(task.start_at) : `截止 ${displayDate(task.due_at)}` }}{{ isAiTask(task) ? ' · AI 安排' : '' }}</span>
    </button>
    <div class="task-right">
      <span v-if="isAiTask(task)" class="tag ai">AI 安排</span>
      <span v-if="willRemind(task, state.clockNow)" class="tag" :title="reminderLabel(task.remind_before_minutes)"><Icon name="bell" :size="11" />提醒</span>
      <span v-if="detailed" class="tag">其他</span>
      <span class="when">{{ taskTime(task) }}</span>
      <button class="quick-edit" type="button" :aria-label="`编辑${task.title}`" @click="state.editorTaskId = task.id; state.editorOpen = true"><Icon name="edit" :size="14" /></button>
    </div>
  </div>
</template>
