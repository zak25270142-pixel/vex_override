import './assets/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { restorePrefs } from './stores/globle'

// 在任何组件挂载前恢复上次的曲线通道配置与订阅意向，保证首屏即为恢复后的状态
restorePrefs()

const app = createApp(App)

app.use(createPinia())
app.mount('#app')