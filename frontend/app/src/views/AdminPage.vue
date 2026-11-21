<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type AccountEvent, type AccountEventGroup, type AdminUser, type AdminUserPage, type UserStatus } from '../api'
import { auth, refreshPendingUsers } from '../auth'
import { ACCOUNT_EVENT_LABELS, ago, stamp } from '../format'
import { state } from '../state'
import Icon from '../components/Icon.vue'

// 交互以 frontend/reference/admin.html 原型为准；每日对话用量与上限在 P5 接入。
type Tab = UserStatus | 'all'
const TABS: [Tab, string][] = [['pending', '待审核'], ['active', '正常'], ['disabled', '已停用'], ['rejected', '已拒绝'], ['all', '全部']]
const STATUS: Record<UserStatus, [string, string]> = { pending: ['待审核', 'yellow'], active: ['正常', 'green'], disabled: ['已停用', 'red'], rejected: ['已拒绝', ''] }
const EVENT_GROUPS: [AccountEventGroup, string][] = [['all', '全部'], ['review', '审核'], ['account', '账号变更'], ['login', '登录']]
const EVENT_TAG: Record<string, string> = {
  user_approved: 'green', user_enabled: 'green', user_rejected: 'red', user_disabled: 'red', user_deleted: 'red',
  login_failed: 'red', login_locked: 'red', password_reset: 'yellow', chat_limit_changed: 'blue', role_changed: 'blue',
}
const QUICK_REASONS = { reject: ['不认识的注册申请', '信息不完整', '重复注册'], disable: ['疑似脚本或滥用', '本人要求停用', '长期不使用'] }

const section = ref<'users' | 'events'>('users')
const tab = ref<Tab>('pending')
const q = ref('')
const page = ref<AdminUserPage | null>(null)
const loadError = ref('')
const busy = ref('')

const eventGroup = ref<AccountEventGroup>('all')
const events = ref<AccountEvent[]>([])
const eventsDone = ref(false)

const drawerUser = ref<AdminUser | null>(null)
const drawerEvents = ref<AccountEvent[]>([])

type Modal =
  | { type: 'reject' | 'disable'; user: AdminUser; reason: string }
  | { type: 'reset'; user: AdminUser; password: string; copied: boolean }
  | { type: 'delete'; user: AdminUser; confirm: string }
const modal = ref<Modal | null>(null)
const modalError = ref('')

const counts = computed(() => page.value?.counts ?? { pending: 0, active: 0, disabled: 0, rejected: 0, all: 0 })
const selfId = computed(() => auth.user?.id)

// 切换筛选或搜索很快时，只采用最后一次请求的结果，避免先发后到的响应覆盖新结果。
let usersSeq = 0
let eventsSeq = 0

async function loadUsers() {
  loadError.value = ''
  const seq = ++usersSeq
  try {
    const result = await api.adminUsers(tab.value, q.value)
    if (seq !== usersSeq) return
    page.value = result
    auth.pendingUsers = result.counts.pending
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '加载失败'
  }
}

async function loadEvents(more = false) {
  loadError.value = ''
  const seq = ++eventsSeq
  try {
    const beforeId = more ? events.value.at(-1)?.id : undefined
    const items = await api.accountEvents({ group: eventGroup.value, beforeId, limit: 50 })
    if (seq !== eventsSeq) return
    events.value = more ? [...events.value, ...items] : items
    eventsDone.value = items.length < 50
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '加载失败'
  }
}

let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(tab, () => { void loadUsers() })
watch(q, () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { void loadUsers() }, 250) })
watch(eventGroup, () => { void loadEvents() })
watch(section, value => { if (value === 'events') void loadEvents() })

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (modal.value) closeModal()
  else drawerUser.value = null
}
onMounted(() => { void loadUsers(); document.addEventListener('keydown', onKeydown) })
onUnmounted(() => { clearTimeout(searchTimer); document.removeEventListener('keydown', onKeydown) })

function jump(target: Tab) { section.value = 'users'; q.value = ''; tab.value = target }

async function openDrawer(user: AdminUser) {
  drawerUser.value = user
  drawerEvents.value = []
  try {
    const [fresh, items] = await Promise.all([api.adminUser(user.id), api.accountEvents({ userId: user.id, limit: 8 })])
    if (drawerUser.value?.id !== user.id) return
    drawerUser.value = fresh
    drawerEvents.value = items
  } catch (error) {
    state.toast = error instanceof Error ? error.message : '加载账号详情失败'
  }
}

function waitText(value: string) {
  const diff = state.clockNow - new Date(value).getTime()
  return diff < 864e5 ? `已等待 ${Math.max(1, Math.round(diff / 36e5))} 小时` : `已等待 ${Math.round(diff / 864e5)} 天`
}

function eventActor(event: AccountEvent) {
  if (event.actor_user_id === null) return '命令行'
  if (event.actor_user_id === event.target_user_id) {
    const ip = event.details?.ip
    return typeof ip === 'string' && ip ? `本人 · ${ip}` : '本人'
  }
  return `操作人 ${event.actor_username ?? `#${event.actor_user_id}`}`
}

function eventDetail(event: AccountEvent) {
  const details = event.details ?? {}
  if (event.event_type === 'email_changed') return `${details.from ?? '无'} → ${details.to ?? '无'}`
  if (event.event_type === 'role_changed') return `${details.from} → ${details.to}`
  return ''
}

/** 执行一个改变账号的操作，完成后刷新列表、角标与抽屉。 */
async function run(key: string, action: () => Promise<unknown>, message: string) {
  if (busy.value) return false
  busy.value = key
  modalError.value = ''
  try {
    await action()
    state.toast = message
    await loadUsers()
    void refreshPendingUsers()
    if (drawerUser.value) {
      const id = drawerUser.value.id
      if (key.startsWith('delete')) drawerUser.value = null
      else void openDrawer(page.value?.items.find(item => item.id === id) ?? drawerUser.value)
    }
    if (section.value === 'events') void loadEvents()
    return true
  } catch (error) {
    const text = error instanceof Error ? error.message : '操作失败，请稍后再试'
    if (modal.value) modalError.value = text
    else state.toast = text
    return false
  } finally {
    busy.value = ''
  }
}

const approve = (user: AdminUser) => run(`approve:${user.id}`, () => api.approveUser(user.id), `已通过 ${user.username}，对方现在可以登录了。`)
const enable = (user: AdminUser) => run(`enable:${user.id}`, () => api.enableUser(user.id), `已启用 ${user.username}。`)

function openModal(value: Modal) { modal.value = value; modalError.value = '' }
function closeModal() {
  const current = modal.value
  if (!current || busy.value) return
  if (current.type === 'reset' && current.password && !current.copied && !window.confirm('还没有复制临时密码，关闭后无法再次查看。确定关闭？')) return
  modal.value = null
}
function overlayClick() {
  // 临时密码弹窗不允许点空白处关闭，避免误关。
  if (modal.value?.type === 'reset' && modal.value.password) return
  closeModal()
}

async function confirmModal() {
  const current = modal.value
  if (!current) return
  const user = current.user
  if (current.type === 'reject' || current.type === 'disable') {
    const reason = current.reason.trim()
    const ok = current.type === 'reject'
      ? await run('modal', () => api.rejectUser(user.id, reason), `已拒绝 ${user.username} 的注册申请。`)
      : await run('modal', () => api.disableUser(user.id, reason), `已停用 ${user.username}，对方所有设备已下线。`)
    if (ok) modal.value = null
  } else if (current.type === 'reset') {
    await run('modal', async () => { current.password = (await api.resetUserPassword(user.id)).temporary_password }, `已重置 ${user.username} 的密码。`)
  } else if (current.type === 'delete' && current.confirm === user.username) {
    const ok = await run(`delete:${user.id}`, () => api.deleteUser(user.id), `已删除 ${user.username}，用户名已释放。`)
    if (ok) modal.value = null
  }
}

async function copyPassword() {
  if (modal.value?.type !== 'reset') return
  try { await navigator.clipboard.writeText(modal.value.password) } catch { /* 浏览器不允许时让用户手动复制 */ }
  modal.value.copied = true
}
</script>

<template>
  <main class="page adm-page">
    <section class="adm-hero">
      <div>
        <div class="page-kicker"><span class="dot"></span>管理 · 仅管理员可见</div>
        <h1 class="page-title">账号管理</h1>
        <p class="lead">审核注册、停用账号和重置密码。你看不到其他人的待办和对话内容。</p>
      </div>
    </section>
    <div class="adm-stats">
      <button type="button" class="adm-stat" :class="{ hl: counts.pending }" @click="jump('pending')"><div class="tiny">待审核</div><div class="num">{{ counts.pending }}<small>个</small></div></button>
      <button type="button" class="adm-stat" @click="jump('active')"><div class="tiny">正常账号</div><div class="num">{{ counts.active }}<small>个</small></div></button>
      <button type="button" class="adm-stat" @click="jump('disabled')"><div class="tiny">已停用</div><div class="num">{{ counts.disabled }}<small>个</small></div></button>
      <button type="button" class="adm-stat" @click="jump('all')"><div class="tiny">全部账号</div><div class="num">{{ counts.all }}<small>个</small></div></button>
    </div>

    <div class="adm-subnav" role="tablist">
      <button type="button" role="tab" :aria-selected="section === 'users'" :class="{ active: section === 'users' }" @click="section = 'users'">账号</button>
      <button type="button" role="tab" :aria-selected="section === 'events'" :class="{ active: section === 'events' }" @click="section = 'events'">账号事件</button>
    </div>
    <div v-if="loadError" class="notice error" role="alert">{{ loadError }}</div>

    <template v-if="section === 'users'">
      <div class="adm-toolbar">
        <div class="adm-tabs" role="tablist">
          <button v-for="[key, label] in TABS" :key="key" type="button" :class="{ active: tab === key }" :aria-pressed="tab === key" @click="tab = key">
            {{ label }}<span v-if="key === 'pending' && counts.pending" class="badge">{{ counts.pending }}</span><span v-else class="n">{{ counts[key] }}</span>
          </button>
        </div>
        <label class="adm-search"><Icon name="search" :size="15" /><input v-model="q" placeholder="搜索用户名或邮箱" aria-label="搜索账号" maxlength="100" /></label>
      </div>
      <div class="adm-list">
        <div class="adm-row head"><div>账号</div><div class="c-status">状态</div><div class="c-when">{{ tab === 'pending' ? '注册' : '活动' }}</div><div style="text-align:right">操作</div></div>
        <div v-for="user in page?.items ?? []" :key="user.id" class="adm-row item" tabindex="0" role="button" :aria-label="`查看 ${user.username}`" @click="openDrawer(user)" @keydown.enter.self="openDrawer(user)">
          <div class="adm-acct">
            <span class="av" :class="{ admin: user.role === 'admin' }">{{ user.username[0].toUpperCase() }}</span>
            <div>
              <div class="nm">{{ user.username }}<span v-if="user.role === 'admin'" class="tag blue">管理员</span><span v-if="user.must_change_password" class="tag yellow" title="管理员已重置密码，等待用户修改">待改密</span></div>
              <div class="em">{{ user.email || '未填写邮箱' }}</div>
            </div>
          </div>
          <div class="c-status"><span class="tag" :class="STATUS[user.status][1]"><span class="pill-dot"></span>{{ STATUS[user.status][0] }}</span></div>
          <div class="adm-when c-when">
            <template v-if="user.status === 'pending'"><b>{{ ago(user.created_at, state.clockNow) }}</b>注册<br>{{ waitText(user.created_at) }}</template>
            <template v-else>最近登录 <b>{{ ago(user.last_login_at, state.clockNow) }}</b><br>注册于 {{ ago(user.created_at, state.clockNow) }}</template>
          </div>
          <div class="adm-ops" @click.stop>
            <span v-if="user.id === selfId" class="tiny">这是你</span>
            <template v-else-if="user.status === 'pending'">
              <button class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'reject', user, reason: '' })">拒绝</button>
              <button class="solid" type="button" :disabled="!!busy" @click="approve(user)"><span v-if="busy === `approve:${user.id}`" class="adm-spin"></span><Icon v-else name="check" :size="15" />通过</button>
            </template>
            <template v-else-if="user.status === 'active'">
              <button class="text-btn" type="button" :disabled="!!busy" @click="openModal({ type: 'reset', user, password: '', copied: false })">重置密码</button>
              <button class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'disable', user, reason: '' })">停用</button>
            </template>
            <template v-else-if="user.status === 'disabled'">
              <button class="text-btn" type="button" :disabled="!!busy" @click="openModal({ type: 'reset', user, password: '', copied: false })">重置密码</button>
              <button class="outline" type="button" :disabled="!!busy" @click="enable(user)"><span v-if="busy === `enable:${user.id}`" class="adm-spin"></span>启用</button>
            </template>
            <button v-else class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'delete', user, confirm: '' })">删除</button>
          </div>
        </div>
        <div v-if="page && !page.items.length" class="adm-empty">
          <template v-if="q.trim()"><b>没有匹配的账号</b>换个关键词试试，当前搜索：“{{ q.trim() }}”</template>
          <template v-else-if="tab === 'pending'"><b>没有待审核的注册</b>新用户提交注册后会出现在这里，导航上的角标也会提示。</template>
          <template v-else><b>这里还没有账号</b></template>
        </div>
        <div v-if="!page && !loadError" class="adm-empty">加载中…</div>
      </div>
      <p v-if="page && page.total > page.items.length" class="tiny adm-more">共 {{ page.total }} 个，只显示前 {{ page.items.length }} 个，请用搜索缩小范围。</p>
    </template>

    <template v-else>
      <div class="adm-toolbar">
        <div class="adm-tabs">
          <button v-for="[key, label] in EVENT_GROUPS" :key="key" type="button" :class="{ active: eventGroup === key }" :aria-pressed="eventGroup === key" @click="eventGroup = key">{{ label }}</button>
        </div>
        <span class="tiny">账号事件只追加，不能修改或删除</span>
      </div>
      <div class="adm-list">
        <div v-for="event in events" :key="event.id" class="adm-ev">
          <span class="tm">{{ stamp(event.created_at) }} · {{ ago(event.created_at, state.clockNow) }}</span>
          <span><span class="tag" :class="EVENT_TAG[event.event_type]">{{ ACCOUNT_EVENT_LABELS[event.event_type] ?? event.event_type }}</span></span>
          <span>{{ event.target_username ?? `#${event.target_user_id}` }}<span v-if="event.reason" class="tiny">　原因：{{ event.reason }}</span><span v-if="eventDetail(event)" class="tiny">　{{ eventDetail(event) }}</span></span>
          <span class="who">{{ eventActor(event) }}</span>
        </div>
        <div v-if="!events.length" class="adm-empty"><b>没有事件</b></div>
      </div>
      <div v-if="events.length && !eventsDone" class="adm-more"><button class="outline" type="button" @click="loadEvents(true)">加载更多</button></div>
    </template>

    <!-- 移到 body 下：.page 的入场动画带 transform，会让 fixed 定位只覆盖 main。 -->
    <Teleport to="body">
    <div v-if="drawerUser" class="drawer-overlay" @click.self="drawerUser = null">
      <section class="drawer adm-drawer" role="dialog" aria-modal="true" aria-labelledby="adm-drawer-title">
        <div class="drawer-head"><span class="tiny">账号详情 / #{{ drawerUser.id }}</span><button class="square-btn" type="button" aria-label="关闭" @click="drawerUser = null"><Icon name="x" :size="17" /></button></div>
        <div class="adm-tags">
          <span class="tag" :class="STATUS[drawerUser.status][1]"><span class="pill-dot"></span>{{ STATUS[drawerUser.status][0] }}</span>
          <span class="tag" :class="drawerUser.role === 'admin' ? 'blue' : 'ghost'">{{ drawerUser.role === 'admin' ? '管理员' : '普通用户' }}</span>
          <span v-if="drawerUser.must_change_password" class="tag yellow">待改密</span>
        </div>
        <h2 id="adm-drawer-title">{{ drawerUser.username }}</h2>
        <div v-if="drawerUser.status === 'pending'" class="adm-note">{{ waitText(drawerUser.created_at) }}。通过后对方即可登录；拒绝后用户名仍被占用，可以再删除释放。</div>
        <div v-if="drawerUser.status_reason && (drawerUser.status === 'disabled' || drawerUser.status === 'rejected')" class="adm-note red">{{ drawerUser.status === 'disabled' ? '停用' : '拒绝' }}原因：{{ drawerUser.status_reason }}</div>
        <dl class="acc-kv">
          <dt>邮箱</dt><dd>{{ drawerUser.email || '未填写' }}</dd>
          <dt>注册时间</dt><dd>{{ stamp(drawerUser.created_at) }}（{{ ago(drawerUser.created_at, state.clockNow) }}）</dd>
          <dt>最近登录</dt><dd>{{ drawerUser.last_login_at ? `${stamp(drawerUser.last_login_at)}（${ago(drawerUser.last_login_at, state.clockNow)}）` : '从未登录' }}</dd>
          <dt v-if="drawerUser.reviewed_at">审核时间</dt><dd v-if="drawerUser.reviewed_at">{{ stamp(drawerUser.reviewed_at) }}</dd>
        </dl>
        <div class="adm-dsec">
          <h3>账号事件 <button class="text-btn" type="button" @click="drawerUser = null; section = 'events'; eventGroup = 'all'">查看全部</button></h3>
          <div class="adm-timeline">
            <div v-for="event in drawerEvents" :key="event.id" class="tl">
              <div class="a">{{ ACCOUNT_EVENT_LABELS[event.event_type] ?? event.event_type }}</div>
              <div class="b">{{ stamp(event.created_at) }} · {{ eventActor(event) }}{{ event.reason ? ` · ${event.reason}` : '' }}{{ eventDetail(event) ? ` · ${eventDetail(event)}` : '' }}</div>
            </div>
            <div v-if="!drawerEvents.length" class="tiny">暂无</div>
          </div>
        </div>
        <div class="adm-dsec"><div class="tiny" style="line-height:1.7">管理员看不到该用户的待办、对话和操作留痕。</div></div>
        <div class="adm-drawer-actions">
          <span v-if="drawerUser.id === selfId" class="tiny">不能在这里停用或重置自己的账号；修改密码请到个人设置。</span>
          <template v-else-if="drawerUser.status === 'pending'">
            <button class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'reject', user: drawerUser, reason: '' })">拒绝</button>
            <button class="solid" type="button" :disabled="!!busy" @click="approve(drawerUser)"><Icon name="check" :size="15" />通过</button>
          </template>
          <template v-else-if="drawerUser.status === 'active'">
            <button class="text-btn" type="button" :disabled="!!busy" @click="openModal({ type: 'reset', user: drawerUser, password: '', copied: false })">重置密码</button>
            <button class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'disable', user: drawerUser, reason: '' })">停用</button>
          </template>
          <template v-else-if="drawerUser.status === 'disabled'">
            <button class="text-btn" type="button" :disabled="!!busy" @click="openModal({ type: 'reset', user: drawerUser, password: '', copied: false })">重置密码</button>
            <button class="outline" type="button" :disabled="!!busy" @click="enable(drawerUser)">启用</button>
          </template>
          <button v-else class="outline danger-outline" type="button" :disabled="!!busy" @click="openModal({ type: 'delete', user: drawerUser, confirm: '' })">删除</button>
        </div>
      </section>
    </div>

    <div v-if="modal" class="overlay" @click.self="overlayClick">
      <section class="modal adm-modal" role="dialog" aria-modal="true" aria-labelledby="adm-modal-title">
        <template v-if="modal.type === 'reject' || modal.type === 'disable'">
          <h2 id="adm-modal-title" class="adm-modal-title">{{ modal.type === 'reject' ? '拒绝注册' : '停用账号' }}：{{ modal.user.username }}</h2>
          <p class="adm-desc">{{ modal.type === 'reject' ? '拒绝后对方无法登录，登录时会看到“注册申请未通过”。用户名仍被占用，之后可以删除这条申请。' : '停用后对方所有已登录的设备会立即下线，数据保留，随时可以重新启用。' }}</p>
          <label class="adm-label" for="adm-reason">原因（可选，会写入账号事件）</label>
          <textarea id="adm-reason" v-model="modal.reason" class="field adm-textarea" maxlength="200" placeholder="写给以后的自己看"></textarea>
          <div class="adm-chips">
            <button v-for="text in QUICK_REASONS[modal.type]" :key="text" type="button" :class="{ on: modal.reason === text }" @click="modal.reason = modal.reason === text ? '' : text">{{ text }}</button>
          </div>
          <p v-if="modalError" class="adm-note red" role="alert">{{ modalError }}</p>
          <div class="modal-actions">
            <button class="outline" type="button" :disabled="!!busy" @click="closeModal">取消</button>
            <button class="solid red" type="button" :disabled="!!busy" @click="confirmModal"><span v-if="busy" class="adm-spin"></span>{{ modal.type === 'reject' ? '确认拒绝' : '确认停用' }}</button>
          </div>
        </template>
        <template v-else-if="modal.type === 'reset' && !modal.password">
          <h2 id="adm-modal-title" class="adm-modal-title">重置密码：{{ modal.user.username }}</h2>
          <p class="adm-desc">系统会生成一个临时密码，并让对方所有设备下线。对方用临时密码登录后必须先设置新密码。<br>临时密码只显示一次，请通过微信等方式私下告诉对方。</p>
          <p v-if="modalError" class="adm-note red" role="alert">{{ modalError }}</p>
          <div class="modal-actions">
            <button class="outline" type="button" :disabled="!!busy" @click="closeModal">取消</button>
            <button class="solid" type="button" :disabled="!!busy" @click="confirmModal"><span v-if="busy" class="adm-spin"></span>生成临时密码</button>
          </div>
        </template>
        <template v-else-if="modal.type === 'reset'">
          <h2 id="adm-modal-title" class="adm-modal-title">临时密码已生成</h2>
          <p class="adm-desc">把下面的密码告诉 <b>{{ modal.user.username }}</b>。<b>关闭后无法再次查看</b>，忘记了只能重新重置。</p>
          <div class="adm-pw"><span>{{ modal.password }}</span><button type="button" @click="copyPassword">{{ modal.copied ? '已复制' : '复制' }}</button></div>
          <div class="modal-actions"><button class="solid" type="button" @click="closeModal">{{ modal.copied ? '完成' : '我已记下' }}</button></div>
        </template>
        <template v-else-if="modal.type === 'delete'">
          <h2 id="adm-modal-title" class="adm-modal-title">删除注册申请：{{ modal.user.username }}</h2>
          <p class="adm-desc">删除后用户名“{{ modal.user.username }}”被释放，可以重新注册。这条申请没有任何业务数据；账号事件仍会保留。</p>
          <label class="adm-label" for="adm-confirm">输入用户名确认</label>
          <input id="adm-confirm" v-model="modal.confirm" class="field" :placeholder="modal.user.username" autocomplete="off" />
          <p v-if="modalError" class="adm-note red" role="alert">{{ modalError }}</p>
          <div class="modal-actions">
            <button class="outline" type="button" :disabled="!!busy" @click="closeModal">取消</button>
            <button class="solid red" type="button" :disabled="!!busy || modal.confirm !== modal.user.username" @click="confirmModal"><span v-if="busy" class="adm-spin"></span>删除</button>
          </div>
        </template>
      </section>
    </div>
    </Teleport>
  </main>
</template>
