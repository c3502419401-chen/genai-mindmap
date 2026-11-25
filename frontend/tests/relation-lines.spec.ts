import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeAll, describe, expect, it } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import { removeRelation } from '../src/mindmap/edits';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 跨节点关系线（`ContentBase.relations`）真的画出来了。
 *
 * 这一组用例守的是三件在"界面不渲染 relations"那一版**全都做不到**的事：
 *
 *  1. store 把 relations 变成画布上的边（`type: 'mindRelation'`，id 前缀 `relation:`）；
 *  2. **真挂载** `@vue-flow/core` 之后 `<g class="vue-flow__edge-mindRelation">` 真的
 *     出现了，且 kind 的中文标签在 DOM 里——`type` 字符串注册错了、或者句柄没开，
 *     都会在这里红；
 *  3. 详情面板能列出参与的关系，删掉之后画布上那条线一起消失。
 *
 * jsdom 缺的两个浏览器 API（`ResizeObserver` / `DOMMatrixReadOnly`）与
 * `canvas-edges.spec.ts` 同一套补法：Vue Flow 量节点要前一个，读缩放要后一个。
 */
beforeAll(() => {
  const scope = globalThis as unknown as Record<string, unknown>;
  scope.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).DOMMatrixReadOnly = class {
    m22 = 1;
  };
});

function mountCanvas(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MindMapCanvas, { global: { plugins: [pinia] } });
}

/** 等 Vue Flow 把节点与边挂完（它自己有一轮测量，nextTick 一次不够）。 */
async function settle(wrapper: VueWrapper) {
  for (let i = 0; i < 4; i += 1) {
    await wrapper.vm.$nextTick();
    await new Promise(resolve => setTimeout(resolve, 0));
  }
}

function treeWithRelations(): MindMapContent {
  return {
    title: '系统架构',
    root: {
      id: 'r',
      label: '系统架构',
      children: [
        { id: 'b1', label: '数据结构', children: [{ id: 'b1a', label: '树结构', children: [] }] },
        { id: 'b2', label: '生成链路', children: [{ id: 'b2a', label: '工具循环', children: [] }] },
        { id: 'b3', label: '检索', children: [{ id: 'b3a', label: '混合检索', children: [] }] },
      ],
    },
    relations: [
      {
        id: 'rel_1', source: 'b1a', target: 'b2a', kind: 'depends_on',
        why: '工具循环按树结构落 op', provenance: 'ai',
      },
      {
        id: 'rel_2', source: 'b2a', target: 'b3a', kind: 'causes',
        why: '检索质量决定生成内容', provenance: 'ai',
      },
    ],
  };
}

/**
 * 装载带关系线的示例树并**全展开**。
 *
 * 三个端点（`b1a` / `b2a` / `b3a`）都在第 2 层。视图层默认只摊开一层，端点被收起时
 * `relationEdges` 找不到坐标就跳过那条线——那是收起的正确行为（线不许画在看不见的
 * 节点上），但这一组用例测的是"relations 这条通道能不能画出来"，所以显式全展开。
 * 收起的读数在 `tests/view-collapse.spec.ts`。
 */
function loadRelations(store: ReturnType<typeof useMindMapStore>): void {
  store.preview(treeWithRelations(), 'right');
  store.expandAllInView();
}

describe('关系线的数据通道', () => {
  it('store 把 relations 转成 mindRelation 类型的边，id 带 relation: 前缀', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    loadRelations(store);

    const relationEdges = store.edges.filter(edge => edge.type === 'mindRelation');
    expect(relationEdges).toHaveLength(2);
    // 前缀必须与父子线（`a->b`）和附加边（`attach:`）分得开：同一个节点对上
    // 三条线可以同时存在。
    expect(relationEdges.map(edge => edge.id).sort())
      .toEqual(['relation:rel_1', 'relation:rel_2']);
    // 渲染读的是 `kind`，不是旧名 `type`。
    expect(relationEdges[0]!.data).toMatchObject({ kind: 'depends_on' });
  });

  it('选中的节点只列出它参与的那几条', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    loadRelations(store);
    store.select('b2a');

    expect(store.contentRelations).toHaveLength(2);
    expect(store.selectedRelations.map(item => item.id).sort())
      .toEqual(['rel_1', 'rel_2']);
  });

  it('没有关系的节点是一个空列表，而不是"全部"', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    loadRelations(store);
    store.select('b1');

    expect(store.selectedRelations).toEqual([]);
  });
});

describe('关系线在画布上真的画出来', () => {
  it('挂载之后出现两个 edge-mindRelation，并带 kind 的中文标签', async () => {
    const wrapper = mountCanvas();
    loadRelations(useMindMapStore());
    await settle(wrapper);

    const groups = wrapper.findAll('g.vue-flow__edge-mindRelation');
    expect(groups).toHaveLength(2);
    // 标签是中文短词（"依赖" / "因果"），不是字段名：读图的人不需要知道 kind 原文。
    const text = wrapper.text();
    expect(text).toContain('依赖');
    expect(text).toContain('因果');
  });

  it('关系线是虚线，且 conflicts 不画箭头', async () => {
    const wrapper = mountCanvas();
    useMindMapStore().preview({
      title: '冲突',
      root: {
        id: 'r', label: '冲突',
        children: [
          { id: 'a', label: '甲', children: [] },
          { id: 'b', label: '乙', children: [] },
        ],
      },
      relations: [
        { id: 'rel_x', source: 'a', target: 'b', kind: 'conflicts', why: '互斥' },
      ],
    }, 'right');
    await settle(wrapper);

    const path = wrapper.find('g.vue-flow__edge-mindRelation path.vue-flow__edge-path');
    expect(path.exists()).toBe(true);
    // 冲突是对称的：画了箭头会让读者去猜方向。
    expect(path.attributes('marker-end')).toBeUndefined();
    expect(path.attributes('style') ?? '').toContain('stroke-dasharray');
  });
});

describe('删掉一条关系', () => {
  it('edits.removeRelation 只动 relations，树一个字节不变', () => {
    const content = treeWithRelations();
    // `removeRelation` 收/返的是 `DiagramContent`（三种形状的联合），但这条用例
    // 注进去的是树，所以窄回 `MindMapContent` 才能看 `root`。
    const next = removeRelation(content, 'rel_1') as MindMapContent;
    expect(next.relations).toHaveLength(1);
    expect(next.root).toBe(content.root);
    // 不存在的 id 返回**同一个对象**：撤销栈与 diff 靠"没变的还是同一个引用"省事。
    expect(removeRelation(next, 'rel_1')).toBe(next);
  });

  it('store.deleteRelation 之后画布上那条线消失', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    loadRelations(store);
    await settle(wrapper);
    expect(wrapper.findAll('g.vue-flow__edge-mindRelation')).toHaveLength(2);

    expect(store.deleteRelation('rel_1')).toBe(true);
    await settle(wrapper);

    expect(store.contentRelations.map(item => item.id)).toEqual(['rel_2']);
    expect(wrapper.findAll('g.vue-flow__edge-mindRelation')).toHaveLength(1);
    // 未知 id 不该改任何东西。
    expect(store.deleteRelation('rel_nope')).toBe(false);
  });
});
