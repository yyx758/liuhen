<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { auth, logout, refreshPendingUsers } from './auth'
import { PASSWORD_PATH } from './router'
import { displayDate, reminderLabel, taskDate, taskTime } from './format'
import { refresh, runAction, state } from './state'
import Icon from './components/Icon.vue'
import TaskDialog from './components/TaskDialog.vue'
import NotificationBell from './components/NotificationBell.vue'

const nav = [
  ['/', '首页'], ['/chat', '对话'], ['/calendar', '日历'],
  ['/tasks', '待办'], ['/library', '资料库'], ['/history', '留痕'],
]
let clock: ReturnType<typeof setInterval> | undefined
const pendingCount = computed(() => state.actions.filter(action => action.status === 'pending' && new Date(action.expires_at).getTime() > state.clockNow).length)
const detail = computed(() => state.tasks.find(task => task.id === state.detailTaskId))
const detailIsAi = computed(() => Boolean(detail.value?.created_by_agent))
const icpRecord = import.meta.env.VITE_ICP_RECORD?.trim() || '赣ICP备2026013160号-1'
const policeRecord = import.meta.env.VITE_POLICE_RECORD?.trim()
const route = useRoute()
const router = useRouter()
// 登录页、改密页使用独立布局，不显示导航和全局弹层。
const isBarePage = computed(() => Boolean(route.meta.bare))
const menuOpen = ref(false)

// 确认了当前用户、且不在独立页面时才加载业务数据，避免未登录时发出注定 401 的请求。
watch(
  () => [auth.user?.id, auth.user?.must_change_password, isBarePage.value] as const,
  ([userId, mustChange, bare]) => { if (userId && !mustChange && !bare) void refresh() },
  { immediate: true },
)
watch(() => route.fullPath, () => { menuOpen.value = false; void refreshPendingUsers() }, { immediate: true })
watch(() => auth.user?.role, () => { void refreshPendingUsers() })
function closeMenu(event: MouseEvent) {
  if (!(event.target as HTMLElement).closest('.user-menu')) menuOpen.value = false
}
onMounted(() => {
  clock = setInterval(() => { state.clockNow = Date.now() }, 1000)
  document.addEventListener('click', closeMenu)
})
onUnmounted(() => {
  if (clock) clearInterval(clock)
  document.removeEventListener('click', closeMenu)
})
async function signOut() {
  menuOpen.value = false
  await logout()
  await router.replace('/login')
}
function closeNotice() { state.error = ''; state.toast = '' }
function fullDate(value: string) {
  const date = new Date(`${value}T12:00:00`)
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 · 周${'日一二三四五六'[date.getDay()]}`
}
async function toggleTask(id: number, status: 'todo' | 'done', version: number) {
  const ok = await runAction(() => api.updateTask(id, version, { status: status === 'done' ? 'todo' : 'done' }), '任务状态已更新。')
  if (ok) state.detailTaskId = null
}
async function deleteTask(id: number, version: number) {
  const ok = await runAction(() => api.deleteTask(id, version), '待办已删除。')
  if (ok) state.detailTaskId = null
}
</script>

<template>
  <div v-if="!isBarePage" class="shell site-shell">
    <header class="topbar">
      <RouterLink to="/" class="brand" aria-label="小记首页"><span class="brand-mark"><Icon name="spark" :size="13" /></span>小记<span class="brand-period">.</span></RouterLink>
      <nav class="nav" aria-label="主导航">
        <RouterLink v-for="[path, label] in nav" :key="path" :to="path" active-class="active">{{ label }}<span v-if="path === '/history' && pendingCount" class="count">{{ pendingCount }}</span></RouterLink>
        <template v-if="auth.user?.role === 'admin'"><span class="nav-sep" aria-hidden="true"></span><RouterLink to="/admin" active-class="active">管理<span v-if="auth.pendingUsers" class="count" title="待审核账号">{{ auth.pendingUsers }}</span></RouterLink></template>
      </nav>
      <div class="top-actions"><NotificationBell /><button class="top-new" type="button" @click="state.editorTaskId = null; state.editorOpen = true"><Icon name="plus" :size="15" />记一笔</button><div class="user-menu">
          <button class="avatar" type="button" aria-haspopup="menu" :aria-expanded="menuOpen" :aria-label="`账号菜单：${auth.user?.username ?? ''}`" @click="menuOpen = !menuOpen">{{ auth.user?.username.slice(0, 1).toUpperCase() }}</button>
          <div v-if="menuOpen" class="user-menu-panel" role="menu">
            <div class="user-menu-who"><b>{{ auth.user?.username }}</b>{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}</div>
            <RouterLink to="/account" role="menuitem">个人设置</RouterLink>
            <RouterLink :to="PASSWORD_PATH" role="menuitem">修改密码</RouterLink>
            <button type="button" role="menuitem" @click="signOut">退出登录</button>
          </div>
        </div></div>
    </header>
    <div v-if="state.error" class="notice error" role="alert">{{ state.error }}<button type="button" aria-label="关闭提示" @click="closeNotice">×</button></div>
    <RouterView />
    <footer class="footer site-footer">
      <div class="footer-info"><span>小记 · 把日子安排得刚刚好</span><span>任务与提案同步保存</span></div>
      <div v-if="icpRecord || policeRecord" class="footer-filing">
        <a v-if="icpRecord" href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">{{ icpRecord }}</a>
        <a v-if="policeRecord" href="https://beian.mps.gov.cn/" target="_blank" rel="noopener noreferrer">{{ policeRecord }}</a>
      </div>
    </footer>
  </div>
  <RouterView v-else />
  <TaskDialog v-if="!isBarePage" />
  <div v-if="!isBarePage && detail" class="drawer-overlay" @click.self="state.detailTaskId = null">
    <section class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <div class="drawer-head"><span class="tiny">待办详情 / 其他</span><button class="close-btn" type="button" aria-label="关闭" @click="state.detailTaskId = null"><Icon name="x" :size="20" /></button></div>
      <span class="tag" :class="detail.status === 'done' ? 'green' : detail.priority === 'high' ? 'red' : 'blue'">{{ detail.status === 'done' ? '已完成' : detail.priority === 'high' ? '高优先级' : '进行中' }}</span><span v-if="detailIsAi" class="tag ai" style="margin-left:7px">AI 安排</span>
      <h2 id="drawer-title">{{ detail.title }}</h2>
      <div class="meta-line"><Icon name="calendar" :size="15" />{{ fullDate(taskDate(detail)) }}</div>
      <div class="meta-line"><Icon name="clock" :size="15" />{{ taskTime(detail) }} {{ detail.start_at ? '开始' : '截止' }} · 预计 {{ detail.duration_minutes || '未设置' }} 分钟</div>
      <div class="detail-actions"><button class="solid" type="button" :disabled="state.busy" @click="toggleTask(detail.id, detail.status, detail.version)"><Icon name="check" :size="16" />{{ detail.status === 'done' ? '标记未完成' : '标记完成' }}</button><button class="outline" type="button" @click="state.editorTaskId = detail.id; state.detailTaskId = null; state.editorOpen = true"><Icon name="edit" :size="15" />编辑</button><button class="text-btn danger" type="button" :disabled="state.busy" @click="deleteTask(detail.id, detail.version)"><Icon name="trash" :size="15" />删除</button></div>
      <div class="detail-section"><h3 class="subheading">任务描述</h3><p class="small" style="line-height:2;white-space:pre-wrap">还没有添加备注。</p></div>
      <div class="detail-section"><h3 class="subheading">子任务 <span class="tiny">0 / 0</span></h3><p class="small">拆分成几个小步骤，会更容易开始。</p><form style="display:flex;gap:7px;margin-top:13px" @submit.prevent="state.toast = '子任务尚未接入后端。'"><input class="field" placeholder="添加一个子任务" required maxlength="90" /><button class="outline" type="submit" aria-label="添加子任务"><Icon name="plus" :size="17" /></button></form></div>
      <div class="detail-section"><h3 class="subheading">关于这条待办</h3><div class="meta-line">分类 · 其他</div><div class="meta-line">优先级 · {{ detail.priority === 'high' ? '高' : detail.priority === 'medium' ? '中' : '低' }}</div><div class="meta-line">创建方式 · {{ detailIsAi ? '小记建议' : '手动添加' }}</div><div class="meta-line">提醒 · {{ reminderLabel(detail.remind_before_minutes) }}</div><div v-if="detail.due_at" class="meta-line">截止时间 · {{ displayDate(detail.due_at) }}</div></div>
    </section>
  </div>
  <div v-if="!isBarePage && state.toast" class="toast" role="status"><span>{{ state.toast }}</span><button type="button" aria-label="关闭" @click="state.toast = ''">×</button></div>
</template>
