<template>
  <a-modal
    v-model:visible="visible"
    title="微信读书授权"
    :footer="false"
    :mask-closable="false"
    width="400px"
    @cancel="clearTimer"
  >
    <div class="qrcode-container">
      <template v-if="loading">
        <a-spin size="large" />
        <p>正在获取二维码...</p>
      </template>
      <template v-else-if="qrcodeUrl">
        <img :src="qrcodeUrl" alt="微信读书授权二维码" />
        <p>请使用微信扫描二维码登录微信读书</p>
      </template>
      <template v-else>
        <a-alert type="error" :message="errorMessage || '二维码获取失败'" />
        <a-button type="primary" @click="startAuth(true)">重试</a-button>
      </template>
    </div>
  </a-modal>
</template>

<script lang="ts" setup>
import { onBeforeUnmount, ref } from 'vue'
import { getWereadLoginStatus, startWereadLogin } from '@/api/weread'

const emit = defineEmits(['success', 'error'])
const visible = ref(false)
const loading = ref(false)
const qrcodeUrl = ref('')
const errorMessage = ref('')
let timer: number | null = null

const clearTimer = () => {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

const pollStatus = () => {
  clearTimer()
  timer = window.setInterval(async () => {
    try {
      const status = await getWereadLoginStatus()
      if (status?.login_status) {
        clearTimer()
        visible.value = false
        emit('success', status)
      } else if (status?.state === 'expired' || status?.state === 'failed') {
        clearTimer()
        qrcodeUrl.value = ''
        errorMessage.value = status.error || '二维码已失效，请重试'
        emit('error', status)
      }
    } catch (error) {
      clearTimer()
      qrcodeUrl.value = ''
      errorMessage.value = String(error)
      emit('error', error)
    }
  }, 2000)
}

const startAuth = async (force = false) => {
  visible.value = true
  loading.value = true
  qrcodeUrl.value = ''
  errorMessage.value = ''
  clearTimer()
  try {
    const status = await startWereadLogin(force)
    if (status?.login_status) {
      visible.value = false
      emit('success', status)
      return
    }
    if (!status?.qr_code) {
      throw new Error(status?.error || '二维码生成失败')
    }
    qrcodeUrl.value = status.qr_code
    pollStatus()
  } catch (error) {
    errorMessage.value = String(error)
    emit('error', error)
  } finally {
    loading.value = false
  }
}

onBeforeUnmount(clearTimer)
defineExpose({ startAuth })
</script>

<style scoped>
.qrcode-container {
  display: flex;
  min-height: 260px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.qrcode-container img {
  width: 200px;
  height: 200px;
}
</style>
