<script setup lang="ts">
import { computed, ref } from 'vue'
import { today } from '../format'
import { state } from '../state'
import Icon from '../components/Icon.vue'

const search = ref('')
function daysAgo(count: number) {
  const date = new Date(`${today()}T12:00:00`)
  date.setDate(date.getDate() - count)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
const examples = [
  { title: 'Agent 评测方法笔记', category: '技术笔记', content: '记录评测集设计、工具调用正确率、结构化输出和边界用例的评判方法。', updated: daysAgo(1) },
  { title: 'Redis 面试复习', category: '学习资料', content: '持久化、缓存穿透与击穿、分布式锁、Redis Stream。', updated: daysAgo(2) },
  { title: '本周复盘', category: '个人记录', content: '本周推进的工作、值得保持的习惯和下周要改进的地方。', updated: daysAgo(4) },
]
const notes = computed(() => examples.filter(note => `${note.title} ${note.content}`.includes(search.value.trim())))
function unavailable() { state.toast = '资料库目前只展示原型示例，文档服务尚未接入。' }
</script>

<template>
  <main class="page">
    <div class="page-head"><div><div class="page-kicker"><span class="dot" /> KNOWLEDGE SPACE</div><h1 class="page-title">资料库</h1><p class="lead">把有用的内容放在一起，随时回来看一看。</p></div><div class="head-actions"><button class="solid" type="button" @click="unavailable"><Icon name="plus" :size="15" />新建笔记</button></div></div>
    <div class="section-heading" style="margin-bottom:24px"><h2 class="section-title">我的资料 <span class="small" style="font-weight:450">{{ examples.length }} 篇</span></h2><label class="search-box"><Icon name="search" :size="16" /><input v-model="search" placeholder="搜索资料" aria-label="搜索资料" /></label></div>
    <div class="library-grid"><button v-for="note in notes" :key="note.title" class="note-card" type="button" @click="unavailable"><span class="tag blue"><Icon name="book" :size="13" />{{ note.category }}</span><h3>{{ note.title }}</h3><p>{{ note.content }}</p><div class="note-bottom">最后更新 · {{ note.updated }}</div></button><div v-if="!notes.length" class="empty-state"><h3>暂无匹配的笔记</h3></div></div>
  </main>
</template>
