<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { login, safeRedirect } from '../auth'

const route = useRoute()
const router = useRouter()
const account = ref('')
const password = ref('')
const remember = ref(false)
const showPassword = ref(false)
const notice = ref('')
const submitting = ref(false)

async function submit() {
  if (submitting.value) return
  submitting.value = true
  notice.value = ''
  try {
    await login(account.value, password.value, remember.value)
    password.value = ''
    // 需要改密时，路由守卫会把用户转到改密页。
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '登录失败，请稍后再试'
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
        <span class="login-register">还没有账号？<RouterLink to="/register" class="login-link">注册</RouterLink></span>
      </header>

      <section class="login-content" aria-labelledby="login-title">
        <h1 id="login-title">欢迎回来</h1>
        <p class="login-intro">登录后继续管理你的待办和资料。</p>

        <form class="login-form" @submit.prevent="submit">
          <label for="login-account">账号</label>
          <input id="login-account" v-model.trim="account" name="username" type="text" autocomplete="username" placeholder="邮箱或用户名" required maxlength="255" />

          <label for="login-password">密码</label>
          <div class="login-password-row">
            <input id="login-password" v-model="password" name="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="输入密码" required maxlength="128" />
            <button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" @click="showPassword = !showPassword">{{ showPassword ? '隐藏' : '显示' }}</button>
          </div>

          <div class="login-options">
            <label class="login-remember"><input v-model="remember" type="checkbox" />保持登录 7 天</label>
            <RouterLink to="/forgot-password">忘记密码</RouterLink>
          </div>

          <button class="login-submit" type="submit" :disabled="submitting">{{ submitting ? '登录中…' : '登录' }}</button>
          <p v-if="notice" class="login-notice" role="alert">{{ notice }}</p>
        </form>

        <p class="login-footnote"><span>小记</span> 做的每一步都会留痕，你随时可以回看。</p>
      </section>
    </div>
  </main>
</template>
