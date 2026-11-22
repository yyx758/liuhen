import { computed, onBeforeUnmount, ref } from 'vue'
import type { EmailCodeSent } from './api'

/** "获取验证码"按钮的发送状态与重发倒计时；注册、找回密码、修改邮箱共用。 */
export function useCodeCountdown() {
  const remaining = ref(0)
  const sending = ref(false)
  let timer: ReturnType<typeof setInterval> | undefined

  function stop() {
    if (timer !== undefined) clearInterval(timer)
    timer = undefined
  }

  function start(seconds: number) {
    stop()
    remaining.value = seconds
    timer = setInterval(() => {
      remaining.value -= 1
      if (remaining.value <= 0) stop()
    }, 1000)
  }

  /** 调用发送接口，成功后按后端返回的 resend_after 倒计时；失败时把错误抛给调用方显示。 */
  async function send(request: () => Promise<EmailCodeSent>) {
    if (sending.value || remaining.value > 0) return
    sending.value = true
    try {
      start((await request()).resend_after)
    } finally {
      sending.value = false
    }
  }

  const disabled = computed(() => sending.value || remaining.value > 0)
  const label = computed(() => sending.value ? '发送中…' : remaining.value > 0 ? `${remaining.value} 秒后重发` : '获取验证码')

  onBeforeUnmount(stop)
  return { send, disabled, label }
}
