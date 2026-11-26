import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { REAL_TREES } from './fixtures/real-trees';
import type { MindMapContent } from '../src/api/types';
import SourceInputs from '../src/components/SourceInputs.vue';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

/**
 * **三个缺陷的现状**（写用户手册时撞到的，2026-11-26）。
 *
 * 这个文件的每一条都是 `it.fails`：它断言的是"用户本来应该能做到的事"，而在修复
 * 之前三条全都不成立。修完之后把 `.fails` 摘掉，它们就变成正式用例——
 * `it.fails` 在"其实已经好了"时会报错，所以忘记摘也会被测试自己喊住。
 *
 * 为什么要有这个文件，而不是分散进各自的 spec：这三条的共同点是**3311 条后端用例与
 * 1376 条前端用例一条都没抓到它们**。它们不是"某个函数写错了"，而是"整条用户路径上
 * 少了一节"——`previewMove` 的每条用例都在一棵没被收缩的树上跑；材料的两个按钮从来
 * 没有人从"用户看着这两个字会怎么理解"这一侧断言过；删除导图这个动作在测试里根本
 * 没有出现过（所以它 405 了很久也没人知道）。
 */

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('缺陷一：删除导图', () => {
  it.fails('前端有删除导图的客户端函数（后端接口 405 时它无从谈起）', async () => {
    const client = await import('../src/api/client');
    expect(typeof (client as Record<string, unknown>).deleteMindMap).toBe('function');
  });
});

describe('缺陷二：移除与删除并排', () => {
  it.fails('两个按钮的文案一眼能分清（现在是「移除」与「删除」）', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const sources = useSourcesStore();
    const wrapper = mount(SourceInputs, { global: { plugins: [pinia] } });

    sources.addText('一份传错的正文');
    const key = sources.items[0].key;
    // 生成过一次 → 后端把临时 id 换成库里的持久 id，从此这一行才有"彻底删除"。
    sources.remap({ [key]: 'lib-1' });
    await wrapper.vm.$nextTick();

    const labels = wrapper.findAll('button').map(button => button.text());
    expect(labels).toContain('从本轮移除');
    expect(labels).toContain('彻底删除');
  });
});

describe('缺陷三：拖到第 2 层节点下面', () => {
  it.fails('在预算截断的树上，落点算得出来（现在是「算不出落点」）', () => {
    const store = useMindMapStore();
    store.preview(REAL_TREES.tree136 as MindMapContent, 'right');
    // 默认预算 40 / 136 个节点：画布只画前 40 个，被拖节点与目标都在这一屏里。
    expect(store.visibleNodeLimit).toBe(40);

    const preview = store.previewMove({
      draggedId: 'n_20728442566641c399a2edb53c1d5494',
      targetId: 'n_11280aea69a4485c9db2fbfc9e61f4d1',
      zone: 'child',
    });

    expect(preview.code).not.toBe('missing');
    expect(preview.ok).toBe(true);
  });
});
