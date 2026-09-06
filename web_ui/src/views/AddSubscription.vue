<template>
  <div class="add-subscription">
    <a-page-header
      title="添加订阅"
      subtitle="添加新的公众号订阅"
      :show-back="true"
      @back="goBack"
    />
    
    <a-card>
      <a-form
        ref="formRef"
        :model="form"
        :rules="rules"
        layout="vertical"
        @submit="handleSubmit"
      >
        <a-alert type="info" style="margin-bottom: 20px">
          粘贴该公众号任意一篇文章链接。系统会识别公众号、加入微信读书书架并创建 RSS 订阅。
        </a-alert>
        <a-form-item label="公众号文章链接" field="url">
          <a-input
            v-model="form.url"
            placeholder="https://mp.weixin.qq.com/s/..."
            allow-clear
          >
            <template #prefix><icon-link /></template>
          </a-input>
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button type="primary" html-type="submit" :loading="loading">
              添加订阅
            </a-button>
            <a-button @click="resetForm">重置</a-button>
          </a-space>
        </a-form-item>
      </a-form>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Message } from '@arco-design/web-vue'
import { subscribeByArticle } from '@/api/subscription'
const router = useRouter()
const loading = ref(false)
const formRef = ref(null)
const form = ref({
  url: '',
})

const rules = {
  url: [
    { required: true, message: '请输入公众号文章链接' },
    {
      pattern: /^https?:\/\/mp\.weixin\.qq\.com\/s\//,
      message: '请输入有效的公众号文章链接',
    },
  ]
}

const handleSubmit = async () => {
  loading.value = true
  try {
    await formRef.value.validate()
  } catch (error) {
    Message.error(error?.errors?.join('\n') || '表单验证失败，请检查输入内容')
    loading.value = false
    return
  }

  try {
    const result = await subscribeByArticle(form.value.url.trim())
    Message.success(result.created ? '订阅添加成功' : '订阅已存在')
    router.push('/')
  } catch (error) {
    console.error('订阅添加失败:', error)
    Message.error(error.message || '订阅添加失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

const resetForm = () => {
  form.value = { url: '' }
}

const goBack = () => {
  router.go(-1)
}
</script>

<style scoped>
.add-subscription {
  padding: 20px;
  max-width: 800px;
  margin: 0 auto;
}

.arco-form-item {
  margin-bottom: 20px;
}
</style>
