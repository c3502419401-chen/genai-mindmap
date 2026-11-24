import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AuthPanel from '../src/components/AuthPanel.vue';
import type { WhoAmI } from '../src/api/types';
import { useAuthStore } from '../src/stores/auth';

/**
 * 账号区：当前身份要说得出，登录/注册/登出要真的发请求。
 *
 * 这一组钉的是三件容易在改版里丢掉的事：
 * 1. **游客不是"没登录"**。门禁只拦匿名，所以"游客"必须显示成一个正常状态，
 *    而不是一句"请先登录"——后者会让用户以为页面坏了。
 * 2. **登录之后要重问一次 `/me`**。登录响应只有 `AuthUser`，`kind`/`guest_id`
 *    只有 `/me` 知道；自己拼状态迟早在某个分支上拼错。
 * 3. **错误要说人话**。401 `auth_bad_credentials` 的文案由后端给，界面直接显示，
 *    不自己造一句"密码错误"——那会与"账号不存在"分成两句，等于告诉攻击者哪一项错了。
 */

const GUEST: WhoAmI = { user: null, guest_id: 'a'.repeat(32), kind: 'guest' };
const SIGNED_IN: WhoAmI = {
  user: { id: 'u1', username: 'alice', created_at: '2026-11-01T00:00:00.000Z' },
  guest_id: null,
  kind: 'user',
};

function jsonReply(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as unknown as Response;
}

/** 记录每次请求，并按顺序给响应。 */
function stubFetch(replies: Record<string, Response[] | Response>) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const queues = new Map<string, Response[]>();
  for (const [key, value] of Object.entries(replies)) {
    queues.set(key, Array.isArray(value) ? [...value] : [value]);
  }
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, method, body });
    for (const [key, queue] of queues) {
      if (url.includes(key) && queue.length) return queue.shift()!;
    }
    return jsonReply({}, 200);
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('身份状态', () => {
  it('没问过之前不猜：既不说游客也不说未登录', () => {
    const auth = useAuthStore();
    expect(auth.loaded).toBe(false);
    expect(auth.label).toBe('正在确认身份…');
    expect(auth.isSignedIn).toBe(false);
  });

  it('问过之后游客是一个正常状态', async () => {
    stubFetch({ '/auth/me': jsonReply(GUEST) });
    const auth = useAuthStore();
    await auth.probe();
    expect(auth.loaded).toBe(true);
    expect(auth.kind).toBe('guest');
    expect(auth.label).toBe('游客');
    expect(auth.isSignedIn).toBe(false);
  });

  it('/me 挂掉时不假装成游客', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('network down');
    }));
    const auth = useAuthStore();
    await auth.probe();
    // 没有证据就不能断言身份：`loaded` 保持 false，界面继续显示"正在确认"。
    expect(auth.loaded).toBe(false);
    expect(auth.label).toBe('正在确认身份…');
  });

  it('登录成功之后重问一次 /me，而不是自己拼状态', async () => {
    const calls = stubFetch({
      '/auth/me': [jsonReply(GUEST), jsonReply(SIGNED_IN)],
      '/auth/login': jsonReply(SIGNED_IN.user),
    });
    const auth = useAuthStore();
    await auth.probe();
    const ok = await auth.signIn('alice', 'password123');
    expect(ok).toBe(true);
    expect(auth.isSignedIn).toBe(true);
    expect(auth.label).toBe('alice');
    expect(calls.filter(call => call.url.includes('/auth/me'))).toHaveLength(2);
    expect(calls.find(call => call.url.includes('/auth/login'))?.body)
      .toEqual({ username: 'alice', password: 'password123' });
  });

  it('登录失败时把后端那句话原样留下', async () => {
    stubFetch({
      '/auth/me': jsonReply(GUEST),
      '/auth/login': jsonReply(
        { code: 'auth_bad_credentials', message: '账号或密码不对。' },
        401,
      ),
    });
    const auth = useAuthStore();
    await auth.probe();
    const ok = await auth.signIn('alice', 'wrong');
    expect(ok).toBe(false);
    expect(auth.error).toBe('账号或密码不对。');
    expect(auth.isSignedIn).toBe(false);
  });

  it('登出之后退回游客，不是匿名', async () => {
    stubFetch({
      '/auth/me': [jsonReply(SIGNED_IN), jsonReply(GUEST)],
      '/auth/logout': jsonReply(null, 204),
    });
    const auth = useAuthStore();
    await auth.probe();
    expect(auth.isSignedIn).toBe(true);
    await auth.signOut();
    expect(auth.kind).toBe('guest');
    expect(auth.label).toBe('游客');
  });
});

describe('账号区的界面', () => {
  it('游客看得到登录与注册两个去向', async () => {
    stubFetch({ '/auth/me': jsonReply(GUEST) });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    expect(wrapper.get('[data-test="auth-who"]').text()).toBe('游客');
    expect(wrapper.get('[data-test="auth-submit"]').text()).toBe('登录');
    await wrapper.get('[data-test="auth-switch"]').trigger('click');
    expect(wrapper.get('[data-test="auth-submit"]').text()).toBe('注册并登录');
    expect(wrapper.text()).toContain('3–32');
  });

  it('用户名或密码空着时不让提交', async () => {
    stubFetch({ '/auth/me': jsonReply(GUEST) });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    expect(wrapper.get('[data-test="auth-submit"]').attributes('disabled')).toBeDefined();
    await wrapper.get('[data-test="auth-username"]').setValue('alice');
    await wrapper.get('[data-test="auth-password"]').setValue('password123');
    expect(wrapper.get('[data-test="auth-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('填完之后点提交会发出登录请求，成功后清空表单', async () => {
    const calls = stubFetch({
      '/auth/me': [jsonReply(GUEST), jsonReply(SIGNED_IN)],
      '/auth/login': jsonReply(SIGNED_IN.user),
    });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    await wrapper.get('[data-test="auth-username"]').setValue('alice');
    await wrapper.get('[data-test="auth-password"]').setValue('password123');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(calls.some(call => call.url.includes('/auth/login'))).toBe(true);
    expect(auth.isSignedIn).toBe(true);
    // 换成了"已登录"那一栏：退出按钮出现，表单消失。
    expect(wrapper.find('[data-test="auth-sign-out"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="auth-submit"]').exists()).toBe(false);
  });

  it('失败时把后端的话显示出来，不自己编一句', async () => {
    stubFetch({
      '/auth/me': jsonReply(GUEST),
      '/auth/login': jsonReply(
        { code: 'auth_rate_limited', message: '失败次数太多了，请 5 秒后再试。' },
        429,
      ),
    });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    await wrapper.get('[data-test="auth-username"]').setValue('alice');
    await wrapper.get('[data-test="auth-password"]').setValue('wrong-one');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('[data-test="auth-error"]').text()).toBe('失败次数太多了，请 5 秒后再试。');
  });

  it('已登录时不显示口令输入框', async () => {
    stubFetch({ '/auth/me': jsonReply(SIGNED_IN) });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    expect(wrapper.get('[data-test="auth-who"]').text()).toBe('alice');
    expect(wrapper.find('[data-test="auth-password"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="auth-sign-out"]').text()).toBe('退出登录');
  });

  it('登录那句提示不许承诺多出来的免费次数', async () => {
    // 免费额度是按 IP 数的一个共享池，游客与账号共用——注册一个账号不会多出次数，
    // 这里写"不再受额度限制"就是在承诺系统不会兑现的东西。
    stubFetch({ '/auth/me': jsonReply(GUEST) });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    const hint = wrapper.text();
    expect(hint).not.toContain('不再受游客额度');
    expect(hint).not.toContain('额度限制');
    expect(hint).toContain('保存到自己名下');
  });

  it('登出之后回到「登录」模式，不是停在退出前那个', async () => {
    stubFetch({
      '/auth/me': [jsonReply(GUEST), jsonReply(SIGNED_IN), jsonReply(GUEST)],
      '/auth/logout': jsonReply({}, 204),
    });
    const auth = useAuthStore();
    await auth.probe();
    const wrapper = mount(AuthPanel);
    // 先切到注册模式，再"登录"成功（这个 stub 的第二次 /me 就是已登录），最后登出。
    await wrapper.get('[data-test="auth-switch"]').trigger('click');
    expect(wrapper.get('[data-test="auth-submit"]').text()).toBe('注册并登录');
    await wrapper.get('[data-test="auth-username"]').setValue('alice');
    await wrapper.get('[data-test="auth-password"]').setValue('password123');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    await wrapper.get('[data-test="auth-sign-out"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="auth-submit"]').text()).toBe('登录');
    expect((wrapper.get('[data-test="auth-username"]').element as HTMLInputElement).value).toBe('');
  });
});
