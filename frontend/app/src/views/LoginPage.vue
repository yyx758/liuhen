<script setup lang="ts">
import { ref } from 'vue'

const account = ref('')
const password = ref('')
const showPassword = ref(false)
const notice = ref('')

function submit() {
  // 后端尚无凭据校验和会话接口，不发送密码，也不伪造登录成功。
  notice.value = '账号密码登录尚未接入，请等待后续鉴权功能。'
}

function unavailable(feature: string) {
  notice.value = `${feature}功能尚未接入。`
}
</script>

<template>
  <main class="login-page">
    <div class="login-shell">
      <header class="login-header">
        <span class="login-brand">小记</span>
        <span class="login-register">还没有账号？<button type="button" @click="unavailable('注册')">注册</button></span>
      </header>

      <section class="login-content" aria-labelledby="login-title">
        <h1 id="login-title">欢迎回来</h1>
        <p class="login-intro">登录后继续管理你的待办和资料。</p>

        <form class="login-form" @submit.prevent="submit">
          <label for="login-account">账号</label>
          <input id="login-account" v-model.trim="account" name="username" type="text" autocomplete="username" placeholder="邮箱或用户名" required />

          <label for="login-password">密码</label>
          <div class="login-password-row">
            <input id="login-password" v-model="password" name="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="输入密码" required />
            <button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" @click="showPassword = !showPassword">{{ showPassword ? '隐藏' : '显示' }}</button>
          </div>

          <div class="login-options">
            <label class="login-remember" title="登录会话接入后开放"><input type="checkbox" disabled />保持登录 7 天</label>
            <button type="button" @click="unavailable('找回密码')">忘记密码</button>
          </div>

          <button class="login-submit" type="submit">登录</button>
          <p v-if="notice" class="login-notice" role="status">{{ notice }}</p>
        </form>

        <p class="login-footnote"><span>小记</span> 做的每一步都会留痕，你随时可以回看。</p>
      </section>
    </div>
  </main>
</template>
