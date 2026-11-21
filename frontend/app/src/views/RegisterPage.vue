<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const username = ref('')
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const notice = ref('')
const submitting = ref(false)
const submitted = ref('')

// 与后端同样的规则（PRD 5.1）；后端仍会再校验一次。
const usernameHint = computed(() => {
  const value = username.value.trim().toLowerCase()
  if (!value) return ''
  return /^[a-z0-9_]{3,32}$/.test(value) ? '' : '用户名须为 3–32 位小写字母、数字或下划线'
})
const passwordHint = computed(() => {
  if (!password.value) return ''
  if (password.value.length < 8 || password.value.length > 128) return '密码长度须为 8–128 位'
  if (password.value.toLowerCase() === username.value.trim().toLowerCase()) return '密码不能与用户名相同'
  return ''
})
const confirmHint = computed(() => confirmPassword.value && confirmPassword.value !== password.value ? '两次输入的密码不一致' : '')

async function submit() {
  if (submitting.value) return
  notice.value = usernameHint.value || passwordHint.value || confirmHint.value
  if (notice.value) return
  submitting.value = true
  try {
    const result = await api.register(username.value.trim(), password.value, email.value.trim() || null)
    submitted.value = result.username
    password.value = ''
    confirmPassword.value = ''
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '注册失败，请稍后再试'
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
        <span class="login-register">已有账号？<RouterLink to="/login" class="login-link">登录</RouterLink></span>
      </header>

      <section v-if="submitted" class="login-content" aria-labelledby="register-done-title">
        <h1 id="register-done-title">已提交</h1>
        <p class="login-intro">注册申请已提交，管理员审核通过后即可用 <b>{{ submitted }}</b> 登录。</p>
        <RouterLink to="/login" class="login-submit register-back">返回登录</RouterLink>
      </section>

      <section v-else class="login-content" aria-labelledby="register-title">
        <h1 id="register-title">创建账号</h1>
        <p class="login-intro">提交后由管理员审核，通过后即可登录。</p>

        <form class="login-form" novalidate @submit.prevent="submit">
          <label for="register-username">用户名</label>
          <input id="register-username" v-model="username" type="text" autocomplete="username" placeholder="3–32 位小写字母、数字或下划线" required maxlength="32" />
          <span v-if="usernameHint" class="register-hint">{{ usernameHint }}</span>

          <label for="register-email" class="login-gap">邮箱（可选）</label>
          <input id="register-email" v-model="email" type="email" autocomplete="email" placeholder="可用于登录" maxlength="255" />

          <label for="register-password" class="login-gap">密码</label>
          <input id="register-password" v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="至少 8 位" required maxlength="128" />
          <span v-if="passwordHint" class="register-hint">{{ passwordHint }}</span>

          <label for="register-confirm" class="login-gap">确认密码</label>
          <input id="register-confirm" v-model="confirmPassword" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" required maxlength="128" />
          <span v-if="confirmHint" class="register-hint">{{ confirmHint }}</span>

          <div class="login-options">
            <label class="login-remember"><input v-model="showPassword" type="checkbox" />显示密码</label>
          </div>

          <button class="login-submit" type="submit" :disabled="submitting">{{ submitting ? '提交中…' : '提交注册' }}</button>
          <p v-if="notice" class="login-notice" role="alert">{{ notice }}</p>
        </form>
      </section>
    </div>
  </main>
</template>
