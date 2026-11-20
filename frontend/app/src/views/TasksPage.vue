<script setup lang="ts">
import { computed, ref } from 'vue'
import { sortTasks, taskDate, today } from '../format'
import { state } from '../state'
import TaskRow from '../components/TaskRow.vue'
import Icon from '../components/Icon.vue'

const filter = ref('all')
const search = ref('')
const filters = [['all', '全部'], ['today', '今天'], ['upcoming', '即将到来'], ['todo', '未完成'], ['done', '已完成']]
const categories = [
  { key: 'job', label: '工作', color: '#5b82e5' },
  { key: 'study', label: '学习', color: '#deb33d' },
  { key: 'life', label: '生活', color: '#49a581' },
  { key: 'other', label: '其他', color: '#9d89bf' },
]
const tasks = computed(() => sortTasks(state.tasks).filter(task => {
  if (!task.title.toLowerCase().includes(search.value.trim().toLowerCase())) return false
  if (filter.value === 'today') return taskDate(task) === today()
  if (filter.value === 'upcoming') return taskDate(task) > today() && task.status === 'todo'
  if (filter.value === 'todo' || filter.value === 'done') return task.status === filter.value
  if (filter.value.startsWith('cat:')) return filter.value === 'cat:other'
  return true
}))
const done = computed(() => state.tasks.filter(task => task.status === 'done').length)
const progress = computed(() => Math.round(done.value / Math.max(state.tasks.length, 1) * 100))
</script>

<template>
  <main class="page">
    <div class="page-head">
      <div><div class="page-kicker"><span class="dot" /> TO-DO LIST</div><h1 class="page-title">待办清单</h1><p class="lead">不急着做完所有事，先把重要的做好。</p></div>
      <div class="head-actions"><button class="solid" type="button" @click="state.editorTaskId = null; state.editorOpen = true"><Icon name="plus" :size="15" />新建待办</button></div>
    </div>
    <div class="main-grid">
      <div>
        <div class="section-heading" style="align-items:center"><h2 class="section-title">我的计划 <span class="small" style="font-weight:450">{{ tasks.length }} 项</span></h2><label class="search-box"><Icon name="search" :size="16" /><input v-model="search" placeholder="搜索待办" aria-label="搜索待办" /></label></div>
        <div class="filters"><button v-for="[key, label] in filters" :key="key" type="button" class="filter-btn" :class="{ active: filter === key }" @click="filter = key">{{ label }}</button></div>
        <div class="tasks-board"><TaskRow v-for="task in tasks" :key="task.id" :task="task" detailed /><div v-if="!tasks.length" class="empty-state"><div class="empty-symbol"><Icon name="search" :size="22" /></div><h3>没有找到对应的待办</h3><p>换个筛选条件，或新建一项吧。</p></div></div>
      </div>
      <aside>
        <div class="info-panel"><h3 class="mini-heading">完成进度</h3><div style="display:flex;align-items:baseline;gap:9px"><b style="font-size:36px;letter-spacing:-1px">{{ progress }}%</b><span class="small">{{ done }} / {{ state.tasks.length }} 已完成</span></div><div class="progress"><span :style="{ width: `${progress}%` }" /></div><p class="small" style="line-height:1.8;margin:16px 0 0">每完成一件小事，都在让计划慢慢成形。</p></div>
        <div class="activity"><h3 class="mini-heading">按类别查看</h3><button v-for="category in categories" :key="category.key" type="button" class="side-choice" :class="{ active: filter === `cat:${category.key}` }" @click="filter = `cat:${category.key}`"><span style="display:flex;gap:9px;align-items:center"><span class="pill-dot" :style="{ background: category.color }" />{{ category.label }}</span><span class="side-num">{{ category.key === 'other' ? state.tasks.length : 0 }}</span></button></div>
      </aside>
    </div>
  </main>
</template>
