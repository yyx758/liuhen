<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, type LoginSession, type OwnEvent } from '../api'
import { auth } from '../auth'
import { useCodeCountdown } from '../codeCountdown'
import { ACCOUNT_EVENT_LABELS, ago, deviceLabel, stamp } from '../format'
import { PASSWORD_PATH } from '../router'
import { state } from '../state'

const sessions = ref<LoginSession[]>([])
const events = ref<OwnEvent[]>([])
const loadError = ref('')
const otherSessions = computed(() => sessions.value.filter(item => !item.current))

// 弹窗：修改 / 验证 / 清除邮箱，或下线设备的二次确认。
type EmailDialog = { type: 'email'; email: string; password: string; code: string }
type Dialog = EmailDialog | { type: 'revoke'; session: LoginSession } | { type: 'revoke-others' }
const dialog = ref<Dialog | null>(null)
const dialogError = ref('')
const dialogInfo = ref('')
const busy = ref(false)
const countdown = useCodeCountdown()

/** 邮箱框留空表示清除邮箱，清除不需要验证码。 */
function clearing(value: EmailDialog) { return !value.email.trim() }

async function sendEmailCode(value: EmailDialog) {
  dialogError.value = value.password ? '' : '请先输入当前密码'
  dialogInfo.value = ''
  if (dialogError.value) return
  try {
    await countdown.send(() => api.ownEmailCode(value.email.trim(), value.password))
    dialogInfo.value = `验证码已发送到 ${value.email.trim()}，10 分钟内有效。`
  } catch (error) {
    dialogError.value = error instanceof Error ? error.message : '验证码发送失败，请稍后再试'
  }
}

async function load() {
  loadError.value = ''
  try {
    ;[sessions.value, events.value] = await Promise.all([api.sessions(), api.ownEvents()])
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '加载失败'
  }
}
onMounted(load)

const savingReminders = ref(false)
async function toggleEmailReminders(event: Event) {
  const input = event.target as HTMLInputElement
  const wanted = input.checked
  savingReminders.value = true
  try {
    const user = await api.setPreferences(wanted)
    if (auth.user) auth.user.email_reminders = user.email_reminders
    state.toast = wanted ? '已开启邮件提醒。' : '已关闭邮件提醒，之后只在页面内提醒。'
  } catch (error) {
    // 保存失败时把开关拨回去，不显示未保存的状态。
    input.checked = !wanted
    state.error = error instanceof Error ? error.message : '保存失败，请稍后再试'
  } finally {
    savingReminders.value = false
  }
}

function open(value: Dialog) { dialog.value = value; dialogError.value = ''; dialogInfo.value = '' }
function close() { if (!busy.value) dialog.value = null }

async function confirmDialog() {
  const current = dialog.value
  if (!current || busy.value) return
  busy.value = true
  dialogError.value = ''
  try {
    if (current.type === 'email') {
      const email = current.email.trim() || null
      const user = await api.changeEmail(email, email ? current.code.trim() : null, current.password)
      if (auth.user) Object.assign(auth.user, { email: user.email, email_verified: user.email_verified })
      state.toast = user.email ? `邮箱 ${user.email} 已验证。` : '邮箱已清除。'
    } else if (current.type === 'revoke') {
      await api.revokeSession(current.session.id)
      state.toast = `已下线 ${deviceLabel(current.session.user_agent)}。`
    } else {
      const { revoked } = await api.revokeOtherSessions()
      state.toast = revoked ? `已退出其他 ${revoked} 台设备。` : '没有其他设备。'
    }
    dialog.value = null
    await load()
  } catch (error) {
    dialogError.value = error instanceof Error ? error.message : '操作失败，请稍后再试'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="page acc-page">
    <section class="page-head">
      <div>
        <div class="page-kicker"><span class="dot"></span>个人设置</div>
        <h1 class="page-title">{{ auth.user?.username }}</h1>
        <p class="lead">管理你的邮箱、密码和已登录的设备。</p>
      </div>
    </section>
    <div v-if="loadError" class="notice error" role="alert">{{ loadError }}</div>

    <section class="acc-section">
      <h2>基本信息</h2>
      <dl class="acc-kv">
        <dt>用户名</dt><dd>{{ auth.user?.username }}<span class="tiny">　用户名不能修改</span></dd>
        <dt>邮箱</dt>
        <dd>
          {{ auth.user?.email || '未填写' }}
          <span v-if="auth.user?.email" class="tag" :class="auth.user.email_verified ? 'green' : 'yellow'">{{ auth.user.email_verified ? '已验证' : '未验证' }}</span>
          <button class="text-btn acc-inline" type="button" @click="open({ type: 'email', email: auth.user?.email ?? '', password: '', code: '' })">{{ auth.user?.email && !auth.user.email_verified ? '验证' : '修改' }}</button>
          <span v-if="!auth.user?.email_verified" class="tiny">　验证邮箱后才能用它找回密码</span>
        </dd>
        <dt>角色</dt><dd>{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}</dd>
        <dt>注册时间</dt><dd>{{ auth.user ? stamp(auth.user.created_at) : '' }}</dd>
        <dt>今日对话</dt><dd v-if="auth.user?.chat_usage">{{ auth.user.chat_usage.limit === null ? `${auth.user.chat_usage.today} 轮 · 不限量` : `${auth.user.chat_usage.today} / ${auth.user.chat_usage.limit} 轮` }}<span class="tiny">　每天 0 点（UTC+8）重新计数</span></dd><dd v-else>—</dd>
        <dt>密码</dt><dd><RouterLink :to="PASSWORD_PATH" class="acc-link">修改密码</RouterLink><span class="tiny">　修改后其他设备会全部下线</span></dd>
        <dt>邮件提醒</dt>
        <dd>
          <label class="acc-switch"><input type="checkbox" :checked="auth.user?.email_reminders" :disabled="savingReminders" @change="toggleEmailReminders" />{{ auth.user?.email_reminders ? '开启' : '关闭' }}</label>
          <span class="tiny">{{ auth.user?.email_verified ? '　待办到点时同时发邮件；页面内提醒始终开启' : '　验证邮箱后才能收到提醒邮件' }}</span>
        </dd>
      </dl>
    </section>

    <section class="acc-section">
      <h2>在线设备 <button v-if="otherSessions.length" class="outline danger-outline" type="button" @click="open({ type: 'revoke-others' })">退出其他所有设备</button></h2>
      <div class="adm-list">
        <div v-for="item in sessions" :key="item.id" class="acc-row">
          <div>
            <div class="acc-name">{{ deviceLabel(item.user_agent) }}<span v-if="item.current" class="tag green">当前设备</span></div>
            <div class="tiny">{{ item.ip || '未知 IP' }} · 登录于 {{ stamp(item.created_at) }} · 最近活动 {{ ago(item.last_seen_at, state.clockNow) }}</div>
          </div>
          <button v-if="!item.current" class="text-btn danger" type="button" @click="open({ type: 'revoke', session: item })">下线</button>
        </div>
        <div v-if="!sessions.length && !loadError" class="adm-empty">加载中…</div>
      </div>
    </section>

    <section class="acc-section">
      <h2>登录记录 <span class="tiny">最近 30 天</span></h2>
      <div class="adm-list">
        <div v-for="event in events" :key="event.id" class="acc-row">
          <div class="acc-name"><span class="tag" :class="{ red: event.event_type === 'login_failed' || event.event_type === 'login_locked', yellow: event.event_type === 'password_reset' }">{{ ACCOUNT_EVENT_LABELS[event.event_type] ?? event.event_type }}</span></div>
          <div class="tiny">{{ stamp(event.created_at) }}{{ event.ip ? ` · ${event.ip}` : '' }}</div>
        </div>
        <div v-if="!events.length" class="adm-empty">暂无记录</div>
      </div>
    </section>

    <!-- 移到 body 下：.page 的入场动画带 transform，会让 fixed 定位只覆盖 main。 -->
    <Teleport to="body">
    <div v-if="dialog" class="overlay" @click.self="close">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="acc-dialog-title">
        <template v-if="dialog.type === 'email'">
          <h2 id="acc-dialog-title" class="adm-modal-title">邮箱</h2>
          <p class="adm-desc">邮箱可以用来登录和找回密码，保存前需要输入当前密码，并填写发到新邮箱的验证码。留空表示清除邮箱，不需要验证码。</p>
          <form id="acc-email-form" @submit.prevent="confirmDialog">
            <label class="adm-label" for="acc-email">邮箱</label>
            <input id="acc-email" v-model="dialog.email" class="field" type="email" maxlength="255" autocomplete="email" placeholder="留空表示清除" />
            <label class="adm-label acc-gap" for="acc-password">当前密码</label>
            <input id="acc-password" v-model="dialog.password" class="field" type="password" maxlength="128" autocomplete="current-password" required />
            <template v-if="!clearing(dialog)">
              <label class="adm-label acc-gap" for="acc-code">邮箱验证码</label>
              <div class="acc-code-row">
                <input id="acc-code" v-model="dialog.code" class="field" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="6 位数字" />
                <button class="outline" type="button" :disabled="countdown.disabled.value" @click="sendEmailCode(dialog)">{{ countdown.label.value }}</button>
              </div>
            </template>
          </form>
          <p v-if="dialogInfo" class="adm-note">{{ dialogInfo }}</p>
        </template>
        <template v-else-if="dialog.type === 'revoke'">
          <h2 id="acc-dialog-title" class="adm-modal-title">下线这台设备？</h2>
          <p class="adm-desc">{{ deviceLabel(dialog.session.user_agent) }}（{{ dialog.session.ip || '未知 IP' }}）下线后需要重新登录。</p>
        </template>
        <template v-else>
          <h2 id="acc-dialog-title" class="adm-modal-title">退出其他所有设备？</h2>
          <p class="adm-desc">除当前设备外，其他 {{ otherSessions.length }} 台设备都会下线。</p>
        </template>
        <p v-if="dialogError" class="adm-note red" role="alert">{{ dialogError }}</p>
        <div class="modal-actions">
          <button class="outline" type="button" :disabled="busy" @click="close">取消</button>
          <button v-if="dialog.type === 'email'" class="solid" type="submit" form="acc-email-form" :disabled="busy || !dialog.password || (!clearing(dialog) && !dialog.code.trim())">{{ busy ? '保存中…' : '保存' }}</button>
          <button v-else class="solid red" type="button" :disabled="busy" @click="confirmDialog">{{ busy ? '处理中…' : '确认下线' }}</button>
        </div>
      </section>
    </div>
    </Teleport>
  </main>
</template>
