<script setup lang="ts">
import { computed, ref } from 'vue';

import { useAuthStore } from '../stores/auth';

/**
 * 抽屉里的账号区：当前身份 + 登录/注册/登出。
 *
 * 为什么放在抽屉而不是顶栏：顶栏只剩三件（`☰` / 标题 / `+`），登录是**低频**动作，
 * 放进那三件里会把最常用的一件挤走。抽屉里已经集中了保存/打开/导入/导出/偏好，
 * 账号和它们同级。
 *
 * 为什么默认是游客而不是"请先登录"：这一版的门禁只拦匿名——新访客点进来就有
 * 游客身份与每天三次免费生成。把登录做成闸门会让"看看这东西能干嘛"变成先交一个
 * 账号，那是另一种产品。
 */
const auth = useAuthStore();

/** `login` 与 `register` 共用一个表单：字段一样，只有提交去向不同。 */
type Mode = 'login' | 'register';
const mode = ref<Mode>('login');
const username = ref('');
const password = ref('');

const submitLabel = computed(() => (mode.value === 'login' ? '登录' : '注册并登录'));
/**
 * 登录那一句**不能**说"不再受游客额度的限制"：免费额度是按 IP 数的一个共享池，
 * 游客和账号共用（注册一个账号不会多出次数），只有自己填了密钥才不受限。
 * 承诺系统不会兑现的东西，比不说更糟——用户会用"我登录了怎么还不行"来回报它。
 */
const modeHint = computed(() => (
  mode.value === 'login'
    ? '登录之后：导图保存到自己名下，别的账号能看不能改。'
    : '用户名 3–32 位字母数字下划线，密码至少 8 位。'
));

function switchMode(next: Mode): void {
  mode.value = next;
  auth.clearError();
}

async function submit(): Promise<void> {
  const name = username.value.trim();
  const pass = password.value;
  if (!name || !pass) return;
  const ok = mode.value === 'login'
    ? await auth.signIn(name, pass)
    : await auth.signUp(name, pass);
  if (ok) {
    username.value = '';
    password.value = '';
  }
}

async function signOut(): Promise<void> {
  await auth.signOut();
  // 退回「登录」而不是停在用户退出前那个模式：刚点了退出的人下一次大概率是
  // 重新登录，不是注册一个新账号。停在「注册」等于把最不可能的动作用主按钮摆着。
  mode.value = 'login';
  username.value = '';
  password.value = '';
}
</script>

<template>
  <section data-test="auth-panel" class="mt-4 border-t border-slate-200 pt-3">
    <div class="flex items-center justify-between">
      <p class="text-[11px] font-medium text-slate-600">账号</p>
      <span
        data-test="auth-who"
        class="rounded px-1.5 py-0.5 text-[11px]"
        :class="auth.isSignedIn
          ? 'bg-emerald-50 text-emerald-700'
          : 'bg-slate-100 text-slate-500'"
      >{{ auth.label }}</span>
    </div>

    <!-- 已登录：只说怎么退出，不再给一个改密码的入口——改密码要旧密码，
         一次改完所有设备都要重登，那是设置页的事，不是抽屉里顺手点的东西。 -->
    <div v-if="auth.isSignedIn" class="mt-2 flex flex-col gap-1.5">
      <p class="leading-relaxed text-[11px] text-slate-400">
        保存的导图都在你名下，别的账号能看不能改。
      </p>
      <p class="leading-relaxed text-[11px] text-slate-400">
        这里没有改密码的入口：改密码要旧密码，而且改完所有设备都要重新登录，那是设置页的事。忘了密码这边也帮不上——仓库里没有发邮件的地方，只能让部署的人直接改库。
      </p>
      <button
        type="button"
        data-test="auth-sign-out"
        class="self-start rounded-md border border-slate-300 px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        :disabled="auth.working"
        @click="signOut"
      >退出登录</button>
    </div>

    <!-- 游客/未识别：给登录与注册两个入口 -->
    <form v-else class="mt-2 flex flex-col gap-1.5" @submit.prevent="submit">
      <p class="leading-relaxed text-[11px] text-slate-400">{{ modeHint }}</p>
      <input
        v-model="username"
        data-test="auth-username"
        type="text"
        autocomplete="username"
        placeholder="用户名"
        class="w-full rounded-md border border-slate-300 px-2 py-1 text-[11px]"
      >
      <input
        v-model="password"
        data-test="auth-password"
        type="password"
        :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
        placeholder="密码"
        class="w-full rounded-md border border-slate-300 px-2 py-1 text-[11px]"
      >
      <div class="flex items-center gap-2">
        <button
          type="submit"
          data-test="auth-submit"
          class="rounded-md bg-slate-900 px-2 py-1 text-[11px] text-white hover:bg-slate-700 disabled:opacity-40"
          :disabled="auth.working || !username.trim() || !password"
        >{{ submitLabel }}</button>
        <button
          type="button"
          data-test="auth-switch"
          class="rounded-md border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
          :disabled="auth.working"
          @click="switchMode(mode === 'login' ? 'register' : 'login')"
        >{{ mode === 'login' ? '没有账号？注册' : '已有账号？登录' }}</button>
      </div>
      <p
        v-if="auth.error"
        data-test="auth-error"
        role="alert"
        class="leading-relaxed text-[11px] text-rose-700"
      >{{ auth.error }}</p>
    </form>
  </section>
</template>
