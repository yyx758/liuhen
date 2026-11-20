import { createRouter, createWebHistory } from 'vue-router'
import HomePage from './views/HomePage.vue'
import ChatPage from './views/ChatPage.vue'
import CalendarPage from './views/CalendarPage.vue'
import TasksPage from './views/TasksPage.vue'
import LibraryPage from './views/LibraryPage.vue'
import AuditPage from './views/AuditPage.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: HomePage },
    { path: '/chat', component: ChatPage },
    { path: '/calendar', component: CalendarPage },
    { path: '/tasks', component: TasksPage },
    { path: '/library', component: LibraryPage },
    { path: '/review', redirect: '/history?filter=pending' },
    { path: '/history', component: AuditPage },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
