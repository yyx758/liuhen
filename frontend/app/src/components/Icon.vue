<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 17 })
</script>

<template>
  <svg class="icon" :style="{ width: `${size}px`, height: `${size}px` }" viewBox="0 0 24 24" aria-hidden="true">
    <use :href="`#i-${name}`" />
  </svg>
</template>
