import { createRouter, createWebHistory } from 'vue-router'
import { auth, loadCurrentUser, safeRedirect } from './auth'
import HomePage from './views/HomePage.vue'
import ChatPage from './views/ChatPage.vue'
import CalendarPage from './views/CalendarPage.vue'
import TasksPage from './views/TasksPage.vue'
import LibraryPage from './views/LibraryPage.vue'
import AuditPage from './views/AuditPage.vue'
import LoginPage from './views/LoginPage.vue'
import PasswordPage from './views/PasswordPage.vue'
import RegisterPage from './views/RegisterPage.vue'
import ForgotPasswordPage from './views/ForgotPasswordPage.vue'
import AccountPage from './views/AccountPage.vue'
import AdminPage from './views/AdminPage.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 不需要登录即可访问。 */
    public?: boolean
    /** 独立页面布局：不显示顶部导航、页脚和全局弹层。 */
    bare?: boolean
    /** 只有管理员能访问。 */
    admin?: boolean
  }
}

export const PASSWORD_PATH = '/account/password'

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', component: LoginPage, meta: { public: true, bare: true } },
    { path: '/register', component: RegisterPage, meta: { public: true, bare: true } },
    { path: '/forgot-password', component: ForgotPasswordPage, meta: { public: true, bare: true } },
    { path: PASSWORD_PATH, component: PasswordPage, meta: { bare: true } },
    { path: '/account', component: AccountPage },
    { path: '/admin', component: AdminPage, meta: { admin: true } },
    { path: '/', component: HomePage },
    { path: '/chat', component: ChatPage },
    { path: '/calendar', component: CalendarPage },
    { path: '/tasks', component: TasksPage },
    { path: '/library', component: LibraryPage },
    { path: '/history', component: AuditPage },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async to => {
  if (!auth.checked) await loadCurrentUser()
  const user = auth.user
  if (!user) {
    if (to.meta.public) return true
    return { path: '/login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  // 管理员重置密码后，改完密码之前只能停留在改密页。
  if (user.must_change_password && to.path !== PASSWORD_PATH) return PASSWORD_PATH
  if (to.path === '/login') return safeRedirect(to.query.redirect)
  if (to.path === '/register' || to.path === '/forgot-password') return '/'
  // 后端仍会对 /admin/* 校验角色，这里只是不让普通用户看到空页面。
  if (to.meta.admin && user.role !== 'admin') return '/'
  return true
})
