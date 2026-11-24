import { createPinia } from 'pinia';
import { createApp } from 'vue';

import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';

import './styles.css';
import App from './App.vue';
import { useAuthStore } from './stores/auth';
import { useMindMapStore } from './stores/mindmap';

const pinia = createPinia();
const app = createApp(App).use(pinia);

/**
 * 先确认身份，**再挂载**。
 *
 * 顺序不能换：整站的门禁只放行"有身份"的请求，而新的游客 cookie 只有
 * `GET /api/auth/me` 会发。先 mount 的话，`App` 挂载时立刻发出的那批请求（偏好、
 * 知识库、可恢复的运行）会赶在 cookie 之前到达后端，一起 401——现象是"页面一打开
 * 就报错，刷新一下又好了"，因为第二次进来 cookie 已经在了。
 *
 * 代价是首屏等一个请求。那个请求不发模型调用、只读一行会话（没会话时连库都不读），
 * 与"白屏一次 + 报错一次"相比是便宜的。失败静默：`probe()` 内部已经咽掉了，它
 * 拿不到身份时 `me` 保持 `null`，界面显示"正在确认身份"而不是断言一个身份。
 *
 * 用 `.then()` 而不是 `await`：顶层 `await` 要求构建目标支持它，而这个项目
 * `target: ES2020`。为两行挂载逻辑去改全局构建目标不划算。
 */
void useAuthStore(pinia).probe().then(() => {
  app.mount('#app');

  // 进页面再问一句"有没有我还没接住的结果"（上次生成完/被中断、但那棵已经落进
  // 画布里的树没保存下来）。
  //
  // 放在入口而不是 `App.vue` 的 `onMounted` 里：这是**一次页面加载一次**的副作用，
  // 与界面生命周期无关；挂在组件上会让每个 mount App 的地方（包括界面测试）都多发
  // 一次请求，而那些测试断言的是"点了按钮发什么请求"——多出来的一条会把它们全部
  // 变成噪声级的失败。失败静默，`probeResumableRun` 内部已经咽掉了。
  void useMindMapStore(pinia).probeResumableRun();
});
