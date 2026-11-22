<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { useCodeCountdown } from '../codeCountdown'

const email = ref('')
const code = ref('')
const password = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const notice = ref('')
const submitting = ref(false)
const done = ref(false)
const countdown = useCodeCountdown()

// 用户名要等后端找到账号才知道，"不能与用户名相同"交给后端校验。
const passwordHint = computed(() => {
  if (!password.value) return ''
  return password.value.length < 8 || password.value.length > 128 ? '密码长度须为 8–128 位' : ''
})
const confirmHint = computed(() => confirmPassword.value && confirmPassword.value !== password.value ? '两次输入的密码不一致' : '')

async function sendCode() {
  notice.value = email.value.trim() ? '' : '请先填写邮箱'
  if (notice.value) return
  try {
    await countdown.send(() => api.passwordResetEmailCode(email.value.trim()))
    // 后端不透露邮箱是否有账号，这里的提示也只能是"如果"。
    notice.value = '如果该邮箱已绑定并验证，验证码会在几分钟内送达（10 分钟内有效）。'
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '验证码发送失败，请稍后再试'
  }
}

async function submit() {
  if (submitting.value) return
  notice.value = (email.value.trim() ? '' : '请填写邮箱') || (code.value.trim() ? '' : '请填写邮箱验证码')
    || (password.value ? '' : '请填写新密码') || passwordHint.value || confirmHint.value
  if (notice.value) return
  submitting.value = true
  try {
    await api.resetPassword(email.value.trim(), code.value.trim(), password.value)
    done.value = true
    password.value = ''
    confirmPassword.value = ''
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '重置失败，请稍后再试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <div class="login-shell">
      <header class="login-header">
        <span class="login-brand">小记</span>
        <span class="login-register">想起来了？<RouterLink to="/login" class="login-link">登录</RouterLink></span>
      </header>

      <section v-if="done" class="login-content" aria-labelledby="forgot-done-title">
        <h1 id="forgot-done-title">已重置</h1>
        <p class="login-intro">密码已重置，所有设备都已退出登录。请使用新密码登录。</p>
        <RouterLink to="/login" class="login-submit register-back">返回登录</RouterLink>
      </section>

      <section v-else class="login-content" aria-labelledby="forgot-title">
        <h1 id="forgot-title">找回密码</h1>
        <p class="login-intro">通过已验证的邮箱重置密码。邮箱未验证的账号，请联系管理员重置。</p>

        <form class="login-form" novalidate @submit.prevent="submit">
          <label for="forgot-email">邮箱</label>
          <div class="login-password-row">
            <input id="forgot-email" v-model="email" type="email" autocomplete="email" placeholder="注册或绑定的邮箱" required maxlength="255" />
            <button type="button" :disabled="countdown.disabled.value" @click="sendCode">{{ countdown.label.value }}</button>
          </div>

          <label for="forgot-code" class="login-gap">邮箱验证码</label>
          <input id="forgot-code" v-model="code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="6 位数字" required maxlength="6" />

          <label for="forgot-password" class="login-gap">新密码</label>
          <input id="forgot-password" v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="至少 8 位" required maxlength="128" />
          <span v-if="passwordHint" class="register-hint">{{ passwordHint }}</span>

          <label for="forgot-confirm" class="login-gap">确认新密码</label>
          <input id="forgot-confirm" v-model="confirmPassword" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" required maxlength="128" />
          <span v-if="confirmHint" class="register-hint">{{ confirmHint }}</span>

          <div class="login-options">
            <label class="login-remember"><input v-model="showPassword" type="checkbox" />显示密码</label>
          </div>

          <button class="login-submit" type="submit" :disabled="submitting">{{ submitting ? '提交中…' : '重置密码' }}</button>
          <p v-if="notice" class="login-notice" role="alert">{{ notice }}</p>
        </form>
      </section>
    </div>
  </main>
</template>
