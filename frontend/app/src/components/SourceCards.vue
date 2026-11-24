<script setup lang="ts">
/** 对话回复下方的来源卡片（spec 4.9）：由服务端生成，点击进入文档详情页并按 D17 定位。全部按纯文本显示。 */
import { RouterLink } from 'vue-router'
import type { DocumentSource } from '../api'
import Icon from './Icon.vue'

defineProps<{ sources: DocumentSource[] }>()

function target(source: DocumentSource) {
  return { path: `/library/${source.document_id}`, query: { h: source.heading_path, q: source.quote, from: 'chat' } }
}
</script>

<template>
  <div class="source-cards">
    <div class="source-label">来源</div>
    <RouterLink v-for="(source, index) in sources" :key="index" class="source-card" :to="target(source)">
      <Icon name="book" :size="15" />
      <span class="source-text">
        <b>{{ source.title }}</b>
        <span v-if="source.heading_path.length" class="source-path">{{ source.heading_path.join(' > ') }}</span>
        <span v-if="source.quote" class="source-quote">“{{ source.quote }}”</span>
      </span>
    </RouterLink>
  </div>
</template>
