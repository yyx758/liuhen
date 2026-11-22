<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, type AppNotification } from '../api'
import { ago, displayDate } from '../format'
import { state } from '../state'
import Icon from './Icon.vue'

// 没有实时推送（SSE 不在本阶段范围）：每 30 秒拉一次未读数，切回页面时立即拉一次。
const POLL_MS = 30_000
const unread = ref(0)
const open = ref(false)
const items = ref<AppNotification[]>([])
const loading = ref(false)
const loadError = ref('')
let timer: ReturnType<typeof setInterval> | undefined

async function refreshCount() {
  if (document.hidden) return
  try {
    unread.value = (await api.unreadNotifications()).count
  } catch {
    // 角标拉取失败不打扰用户，下一轮再试。
  }
}

async function toggle() {
  open.value = !open.value
  if (!open.value) return
  loading.value = true
  loadError.value = ''
  try {
    items.value = await api.notifications()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '通知加载失败'
  } finally {
    loading.value = false
  }
  void refreshCount()
}

async function select(item: AppNotification) {
  open.value = false
  if (!item.read_at) {
    try {
      await api.readNotification(item.id)
      item.read_at = new Date().toISOString()
      unread.value = Math.max(0, unread.value - 1)
    } catch (error) {
      state.error = error instanceof Error ? error.message : '标记已读失败'
    }
  }
  if (state.tasks.some(task => task.id === item.task_id)) state.detailTaskId = item.task_id
  else state.toast = `「${item.task_title}」这条待办已被删除。`
}

async function readAll() {
  try {
    await api.readAllNotifications()
    const now = new Date().toISOString()
    items.value.forEach(item => { item.read_at ??= now })
    unread.value = 0
  } catch (error) {
    state.error = error instanceof Error ? error.message : '标记已读失败'
  }
}

function closeOnOutside(event: MouseEvent) {
  if (!(event.target as HTMLElement).closest('.bell')) open.value = false
}

onMounted(() => {
  void refreshCount()
  timer = setInterval(refreshCount, POLL_MS)
  document.addEventListener('visibilitychange', refreshCount)
  document.addEventListener('click', closeOnOutside)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  document.removeEventListener('visibilitychange', refreshCount)
  document.removeEventListener('click', closeOnOutside)
})
</script>

<template>
  <div class="bell">
    <button class="bell-btn" type="button" aria-haspopup="dialog" :aria-expanded="open" :aria-label="unread ? `通知，${unread} 条未读` : '通知'" @click="toggle">
      <Icon name="bell" :size="18" /><span v-if="unread" class="bell-count">{{ unread > 99 ? '99+' : unread }}</span>
    </button>
    <div v-if="open" class="bell-panel" role="dialog" aria-label="通知">
      <div class="bell-head"><b>通知</b><button v-if="items.some(item => !item.read_at)" class="text-btn" type="button" @click="readAll">全部已读</button></div>
      <div v-if="loading" class="bell-empty">加载中…</div>
      <div v-else-if="loadError" class="bell-empty">{{ loadError }}</div>
      <div v-else-if="!items.length" class="bell-empty">还没有提醒。在待办里设置"提醒"后，到点会出现在这里。</div>
      <button v-for="item in items" v-else :key="item.id" class="bell-item" :class="{ unread: !item.read_at }" type="button" @click="select(item)">
        <span class="bell-title">{{ item.task_title }}<span v-if="item.kind === 'missed'" class="tag yellow">已错过</span></span>
        <span class="tiny">{{ displayDate(item.task_time) }} · {{ ago(item.created_at, state.clockNow) }}提醒</span>
      </button>
    </div>
  </div>
</template>
