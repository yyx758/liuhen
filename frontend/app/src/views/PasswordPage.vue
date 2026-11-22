<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '../api'
import { auth, loadCurrentUser, logout } from '../auth'

const router = useRouter()
const forced = computed(() => Boolean(auth.user?.must_change_password))
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const notice = ref('')
const submitting = ref(false)

async function submit() {
  if (submitting.value) return
  notice.value = ''
  if (newPassword.value.length < 8) { notice.value = '新密码至少 8 位。'; return }
  if (newPassword.value !== confirmPassword.value) { notice.value = '两次输入的新密码不一致。'; return }
  submitting.value = true
  try {
    await api.changePassword(oldPassword.value, newPassword.value)
    await loadCurrentUser()
    await router.replace({ path: '/', query: {} })
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '修改失败，请稍后再试'
  } finally {
    submitting.value = false
  }
}

async function signOut() {
  await logout()
  await router.replace('/login')
}
</script>

<template>
  <main class="login-page">
    <div class="login-shell">
      <header class="login-header">
        <span class="login-brand">小记</span>
        <span class="login-register">
          <RouterLink v-if="!forced" to="/" class="login-link">返回</RouterLink>
          <button v-else type="button" @click="signOut">退出登录</button>
        </span>
      </header>

      <section class="login-content" aria-labelledby="password-title">
        <h1 id="password-title">{{ forced ? '设置新密码' : '修改密码' }}</h1>
        <p class="login-intro">
          {{ forced ? '管理员已重置你的密码。请先设置新密码，之后才能继续使用。' : '修改后，其他设备上的登录会全部失效。' }}
        </p>

        <form class="login-form" @submit.prevent="submit">
          <label for="password-old">{{ forced ? '临时密码' : '当前密码' }}</label>
          <input id="password-old" v-model="oldPassword" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" required maxlength="128" />

          <label for="password-new" class="login-gap">新密码</label>
          <input id="password-new" v-model="newPassword" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="至少 8 位" required minlength="8" maxlength="128" />

          <label for="password-confirm" class="login-gap">再次输入新密码</label>
          <input id="password-confirm" v-model="confirmPassword" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" required maxlength="128" />

          <div class="login-options">
            <label class="login-remember"><input v-model="showPassword" type="checkbox" />显示密码</label>
          </div>

          <button class="login-submit" type="submit" :disabled="submitting">{{ submitting ? '保存中…' : '保存新密码' }}</button>
          <p v-if="notice" class="login-notice" role="alert">{{ notice }}</p>
        </form>
      </section>
    </div>
  </main>
</template>
