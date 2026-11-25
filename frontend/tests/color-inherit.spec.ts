import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import MindNode from '../src/canvas/MindNode.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore, type MindNodeData } from '../src/stores/mindmap';

/**
 * 颜色继承：一级分支一个色，它的所有后代用同一个色。
 *
 * 这是"按分支分组"的实现方式（思维导图的标准做法）：每个节点各一色会像调色板
 * 测试，颜色不再指示"这属于哪一支"。
 *
 * 选择的是**渲染时继承**而不是生成时给每个节点填色，理由就在这组用例里：
 *   - 旧图（库里早就存着、二级往下没有 color 的树）同样有颜色；
 *   - 用户手工改一级分支的颜色时整支跟着变。
 * 生成时填只在"那一次生成"正确。
 */

const TREE: MindMapContent = {
  title: '带色分支',
  root: {
    id: 'r', label: '根', children: [
      {
        id: 'a', label: '甲', color: 'blue', children: [
          {
            id: 'a1', label: '甲一', children: [
              { id: 'a2', label: '甲一一', children: [] },
            ],
          },
        ],
      },
      { id: 'b', label: '乙', color: 'red', children: [{ id: 'b1', label: '乙一', children: [] }] },
      { id: 'c', label: '丙', children: [{ id: 'c1', label: '丙一', children: [] }] },
    ],
  },
};

function colors(store: ReturnType<typeof useMindMapStore>) {
  return new Map(store.nodes.map(item => [item.id, item.data?.color]));
}

/**
 * 装载示例树并**全展开**。
 *
 * 这一组用例看的是整棵树上的颜色继承（三级节点、中间层覆盖、整支重绘），与"画布
 * 默认摊开几层"是两件事。收起是视图层的默认值（`canvas/view-collapse.ts`），在用例里
 * 显式关掉它，断言的才是继承规则本身——否则三级节点根本不在 `store.nodes` 里，
 * 用例会以"颜色丢了"的样子失败，而颜色一直是好的。
 */
function loadTree(content: MindMapContent = structuredClone(TREE)) {
  const store = useMindMapStore();
  store.preview(content, 'right');
  store.expandAllInView();
  return store;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('渲染时沿父链继承', () => {
  it('三级节点沿父链拿到一级分支的颜色', () => {
    const store = loadTree();

    const byId = colors(store);
    expect(byId.get('a')).toBe('blue');
    // 二级、三级自己都没有 color（生成路径只给一级分支填），渲染时继承下来
    expect(byId.get('a1')).toBe('blue');
    expect(byId.get('a2')).toBe('blue');
    // 另一支不受影响
    expect(byId.get('b')).toBe('red');
    expect(byId.get('b1')).toBe('red');
  });

  it('没有色的一级分支整支无色（继承不是"凭空给一个色"）', () => {
    const store = loadTree();

    const byId = colors(store);
    expect(byId.get('c')).toBeUndefined();
    expect(byId.get('c1')).toBeUndefined();
    // 根节点也没有
    expect(byId.get('r')).toBeUndefined();
  });

  it('中间层自己有色时，取**最近的**有色祖先', () => {
    const tree = structuredClone(TREE);
    // 甲一被单独染成绿色：甲一一 应该跟绿色，而不是继续跟一级分支的蓝
    tree.root.children[0].children[0].color = 'green';
    const store = loadTree(tree);

    const byId = colors(store);
    expect(byId.get('a')).toBe('blue');
    expect(byId.get('a1')).toBe('green');
    expect(byId.get('a2')).toBe('green');
  });

  it('侧栏改一级分支的颜色，整支跟着变', () => {
    const store = loadTree();
    expect(colors(store).get('a2')).toBe('blue');

    store.setNodeVisualById('a', { color: 'purple' });

    const byId = colors(store);
    expect(byId.get('a')).toBe('purple');
    // 这正是"渲染时继承"换来的东西：一次改动，整支重绘
    expect(byId.get('a1')).toBe('purple');
    expect(byId.get('a2')).toBe('purple');
    // 别的分支一动不动
    expect(byId.get('b1')).toBe('red');
  });

  it('清掉一级分支的颜色，整支回落成无色', () => {
    const store = loadTree();

    store.setNodeVisualById('a', { color: null });

    const byId = colors(store);
    expect(byId.get('a')).toBeUndefined();
    expect(byId.get('a1')).toBeUndefined();
    expect(byId.get('a2')).toBeUndefined();
  });

  it('撤销之后颜色跟着回到上一版', () => {
    const store = loadTree();
    store.setNodeVisualById('a', { color: 'teal' });
    expect(colors(store).get('a2')).toBe('teal');

    store.undo();

    expect(colors(store).get('a2')).toBe('blue');
  });
});

describe('画布节点真的画出继承来的颜色', () => {
  it('三级节点的 data-node-color / data-node-depth 都由继承结果决定', () => {
    const store = loadTree();
    const data = store.nodes.find(item => item.id === 'a2')!.data as MindNodeData;

    // 句柄要 Vue Flow 的节点上下文才画得出来，这一条只验样式属性：把 in/out 清空
    // 挂载，其余（label / color / depth）全部用 store 真算出来的那一份。
    const wrapper = mount(MindNode, {
      props: { id: 'a2', data: { ...data, in: [], out: [] } },
    });
    const box = wrapper.get('[data-node-color]');
    expect(box.attributes('data-node-color')).toBe('blue');
    // 深度仍是它自己的（根 0 → 甲 1 → 甲一 2 → 甲一一 3）：继承的只有颜色，
    // 样式规则要靠"深度 + 色"两件事一起决定染多少。
    expect(data.depth).toBe(3);
    expect(box.attributes('data-node-depth')).toBe('3');
  });
});

describe('生成中的预览不着色', () => {
  it('还在长的预览不画颜色：它还没进画布', () => {
    const store = useMindMapStore();
    store.newMap();
    // 预览是唯一"看得见但还没进画布"的树（候选层已经删掉了）。
    store.agentPreview = {
      label: '根', expandable: null,
      children: [{ label: '甲', expandable: null, children: [] }],
    };

    const byId = colors(store);
    // 结构照样画出来（预览要看得见），但配色不画：弱化样式与配色会互相打架，
    // "这棵树还在长"这件事必须比"哪一支是什么色"更显眼。
    expect(byId.size).toBeGreaterThan(0);
    expect([...byId.values()].every(value => value === undefined)).toBe(true);
  });
});
