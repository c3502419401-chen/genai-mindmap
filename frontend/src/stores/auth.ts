import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { login, logout, register, whoAmI } from '../api/client';
import type { AuthUser, WhoAmI } from '../api/types';

/**
 * 当前身份（`/api/auth/*`）。
 *
 * 三件事必须说清，否则这一层会被用错：
 *
 * 1. **`guest` 也是身份**。门禁只拦 `anonymous`，所以"没登录"不等于"没身份"——
 *    页面启动时调一次 `/me` 就会拿到一个游客 cookie，之后所有请求都带着它。少了
 *    这一步，整站会以 401 回应一个看起来完全正常的新访客。
 * 2. **`/me` 永远 200**，未登录时 `user` 是 `null`。所以这里不用异常表示"没登录"，
 *    否则"没登录"和"网络坏了"会落进同一个分支。
 * 3. **登录态只存在于 HttpOnly cookie 里**，前端拿不到也存不了 token。于是"我还
 *    登录着吗"每次都得问后端——这个 store 缓存的是**上一次问到的答案**，不是真相。
 *    拿它做权限判断之前先想一遍：真正拦人的是后端。
 */

/** 界面上要显示的状态：已登录 / 游客 / 还没问到。 */
export type AuthKind = 'user' | 'guest' | 'anonymous';

export const useAuthStore = defineStore('auth', () => {
  /** 上一次 `/me` 的答案。`null` = 还没问过（不是"没登录"）。 */
  const me = ref<WhoAmI | null>(null);
  const loading = ref(false);
  /**
   * 注册/登录/登出在途。
   *
   * 与 `loading` 分开：两个状态对界面含义不同——`loading` 期间显示"正在确认身份"，
   * 而 `working` 期间按钮要禁用（重复提交会真的再试一次口令，登录还有速率限制）。
   */
  const working = ref(false);
  const error = ref('');

  const user = computed<AuthUser | null>(() => me.value?.user ?? null);
  const kind = computed<AuthKind>(() => me.value?.kind ?? 'anonymous');
  const isSignedIn = computed(() => kind.value === 'user');
  const loaded = computed(() => me.value !== null);

  /** 顶栏/抽屉上那一行字。没问到之前不猜。 */
  const label = computed(() => {
    if (!me.value) return '正在确认身份…';
    if (user.value) return user.value.username;
    return kind.value === 'guest' ? '游客' : '未识别';
  });

  function apply(next: WhoAmI): void {
    me.value = next;
    error.value = '';
  }

  /**
   * 问一次"我是谁"。顺带**领游客 cookie**——全站只有 `/me` 会发它。
   *
   * 失败不抛：进页面时后端可能还没起来，那时弹一句"身份确认失败"除了吓人没有用。
   * 但 `me` 保持 `null`，界面显示"正在确认身份"而不是"游客"——后者是在没有证据
   * 的情况下做出的断言。
   */
  async function probe(): Promise<void> {
    if (loading.value) return;
    loading.value = true;
    try {
      apply(await whoAmI());
    } catch {
      me.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function signIn(username: string, password: string): Promise<boolean> {
    working.value = true;
    error.value = '';
    try {
      // 登录响应是 `AuthUser`，不含 `kind`/`guest_id`——那两项只有 `/me` 知道。
      // 所以拿它只用来判"成功没成功"（失败会抛），状态一律重问一次，不自己拼。
      await login({ username, password });
      apply(await whoAmI());
      return true;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '登录失败。';
      return false;
    } finally {
      working.value = false;
    }
  }

  async function signUp(username: string, password: string): Promise<boolean> {
    working.value = true;
    error.value = '';
    try {
      await register({ username, password });
      apply(await whoAmI());
      return true;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '注册失败。';
      return false;
    } finally {
      working.value = false;
    }
  }

  /**
   * 登出。**登出之后身份退回游客**，不是匿名——会话 cookie 没了，游客 cookie 还在，
   * 所以下一次 `/me` 会给出 `guest`。这也是用户预期的："我退出登录，不是被赶走"。
   */
  async function signOut(): Promise<void> {
    working.value = true;
    error.value = '';
    try {
      await logout();
      apply(await whoAmI());
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '登出失败。';
    } finally {
      working.value = false;
    }
  }

  function clearError(): void {
    error.value = '';
  }

  return {
    me,
    loading,
    working,
    error,
    user,
    kind,
    isSignedIn,
    loaded,
    label,
    probe,
    signIn,
    signUp,
    signOut,
    clearError,
  };
});
