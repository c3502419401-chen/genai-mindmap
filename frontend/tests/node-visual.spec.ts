import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import MindNode from '../src/canvas/MindNode.vue';
import type { MindMapContent } from '../src/api/types';
import { diffCandidate, useMindMapStore } from '../src/stores/mindmap';
import { isNodeColor, NODE_COLORS, NODE_COLOR_LABELS, validateDraft } from '../src/mindmap/edits';
import { applyOps, OpError } from '../src/mindmap/ops';

/** 一棵带视觉元素的树：一级分支有颜色 + 图标，子节点没有。 */
const VISUAL_MAP: MindMapContent = {
  title: '带样式',
  root: {
    id: 'r', label: '根', children: [
      {
        id: 'a', label: '选题', color: 'blue', icon: '🎯', children: [
          { id: 'a1', label: '范围', children: [] },
        ],
      },
      { id: 'b', label: '风险', color: 'red', children: [] },
      { id: 'c', label: '其他', children: [] },
    ],
  },
};

function mountNode(data: Record<string, unknown>) {
  return mount(MindNode, {
    props: {
      id: 'n',
      data: {
        label: '申请与复核',
        body: '正文',
        fullLabel: '申请与复核',
        depth: 1,
        out: [],
        in: [],
        ...data,
      } as never,
    },
  });
}

describe('调色板（前端这份运行时清单）', () => {
  it('八个键与后端一致，且每个都有中文名', () => {
    expect([...NODE_COLORS]).toEqual([
      'blue', 'green', 'amber', 'red', 'purple', 'teal', 'pink', 'gray',
    ]);
    for (const key of NODE_COLORS) {
      // 下拉框里露出来的是人话，不是 key：用户在界面上不该读到 `amber`
      expect(NODE_COLOR_LABELS[key]).toBeTruthy();
      expect(NODE_COLOR_LABELS[key]).not.toBe(key);
    }
  });

  it('isNodeColor 只认调色板键', () => {
    expect(isNodeColor('blue')).toBe(true);
    expect(isNodeColor('Blue')).toBe(false);
    expect(isNodeColor('#2563eb')).toBe(false);
    expect(isNodeColor(undefined)).toBe(false);
  });
});

describe('画布节点渲染', () => {
  it('颜色挂成 data 属性，不把色值写进样式', () => {
    const wrapper = mountNode({ color: 'amber' });
    const box = wrapper.find('[data-node-color]');
    expect(box.exists()).toBe(true);
    expect(box.attributes('data-node-color')).toBe('amber');
    // 深度也带过去：根节点的样式规则与其余节点不同（只染左边一道）
    expect(box.attributes('data-node-depth')).toBe('1');
    // 关键：色值必须留在 CSS 变量里。写进 style 就等于把亮色主题的结论
    // 固化进数据，换暗色主题时改不回来。
    expect(box.attributes('style') ?? '').not.toContain('#');
  });

  it('没有颜色时不挂 data-node-color（旧图保持原样式）', () => {
    const wrapper = mountNode({});
    expect(wrapper.find('[data-node-color]').exists()).toBe(false);
  });

  it('图标画在标题前面', () => {
    const wrapper = mountNode({ icon: '🎯' });
    const text = wrapper.find('.line-clamp-2').text();
    expect(text.startsWith('🎯')).toBe(true);
    expect(text).toContain('申请与复核');
  });

  it('没有图标时不会留下一个空 span', () => {
    const wrapper = mountNode({});
    expect(wrapper.find('.line-clamp-2').text()).toBe('申请与复核');
  });

  it('候选改动过的节点带一圈琥珀色提示', () => {
    const wrapper = mountNode({ changed: true });
    const box = wrapper.find('.line-clamp-2').element.parentElement!;
    expect(box.className).toContain('ring-amber-300');
  });

  it('编辑态下不渲染图标（输入框里只有文字）', () => {
    const wrapper = mount(MindNode, {
      props: {
        id: 'n',
        editing: true,
        data: {
          label: '标题', body: '标题', fullLabel: '标题', depth: 1,
          out: [], in: [], icon: '🎯',
        } as never,
      },
    });
    expect(wrapper.find('input').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('🎯');
  });
});

describe('store：样式改动', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('设颜色 / 图标，并可撤销', () => {
    const store = useMindMapStore();
    store.preview(structuredClone(VISUAL_MAP), 'right');

    store.setNodeVisualById('c', { color: 'teal', icon: '📊' });
    expect(store.content!.root.children[2].color).toBe('teal');
    expect(store.content!.root.children[2].icon).toBe('📊');
    // 与改标题同一套语义：这是一次可撤销的内容修改
    expect(store.dirty).toBe(true);
    expect(store.canUndo).toBe(true);

    store.undo();
    expect(store.content!.root.children[2].color).toBeUndefined();
  });

  it('清空颜色与图标（null = 显式清除）', () => {
    const store = useMindMapStore();
    store.preview(structuredClone(VISUAL_MAP), 'right');
    store.setNodeVisualById('a', { color: null, icon: null });
    expect(store.content!.root.children[0].color).toBeNull();
    expect(store.content!.root.children[0].icon).toBeNull();
  });

  it('值没变就不记历史', () => {
    const store = useMindMapStore();
    store.preview(structuredClone(VISUAL_MAP), 'right');
    store.setNodeVisualById('a', { color: 'blue' });
    expect(store.canUndo).toBe(false);
  });

  it('图标按码点截到 16，纯空白归一成 null', () => {
    const store = useMindMapStore();
    store.preview(structuredClone(VISUAL_MAP), 'right');
    store.setNodeVisualById('c', { icon: '★'.repeat(40) });
    expect(Array.from(store.content!.root.children[2].icon!).length).toBe(16);
    store.setNodeVisualById('c', { icon: '   ' });
    expect(store.content!.root.children[2].icon).toBeNull();
  });

  it('画布数据里带着颜色与图标（只有已应用的那棵树才画）', () => {
    const store = useMindMapStore();
    store.preview(structuredClone(VISUAL_MAP), 'right');
    // 这条用例读的是**整棵树**在画布数据里的样子（含二级节点继承来的色）。
    // 视图层默认只摊开一层，显式全展开——收起是视图层的默认值，与"颜色有没有
    // 传到 data 里"无关（见 `canvas/view-collapse.ts`）。
    store.expandAllInView();
    const byId = new Map(store.nodes.map(item => [item.id, item.data]));
    expect(byId.get('a')!.color).toBe('blue');
    expect(byId.get('a')!.icon).toBe('🎯');
    // 子节点自己没有颜色，但**渲染时沿父链继承**一级分支的色（见
    // `inheritedColor`）。这条断言原来钉的是 `undefined`——那时颜色只到一级，
    // 二级往下在画布上是灰的，一支配色看不出来。
    expect(byId.get('a1')!.color).toBe('blue');
    // 没有色的一级分支整支都没有色：继承不等于"凭空给一个色"
    expect(byId.get('c')!.color).toBeUndefined();
  });
});

describe('候选 diff（对比两份树）', () => {
  it('新增 / 删除 / 修改各一条，修改里写清改了什么', () => {
    const before = VISUAL_MAP.root;
    const after = {
      id: 'r', label: '根', children: [
        { id: 'a', label: '选题与范围', color: 'purple', children: [{ id: 'a1', label: '范围', children: [] }] },
        { id: 'b', label: '风险', color: 'red', children: [] },
        { id: 'd', label: '新增', children: [] },
      ],
    } as typeof before;

    const changes = diffCandidate(before, after)!;
    const kinds = changes.map(item => `${item.kind}:${item.id}`).sort();
    expect(kinds).toContain('updated:a');
    expect(kinds).toContain('added:d');
    expect(kinds).toContain('removed:c');
    const updated = changes.find(item => item.id === 'a')!;
    expect(updated.detail).toContain('标题');
    expect(updated.detail).toContain('颜色 blue → purple');
  });

  it('移动会单独报一条（父节点变了）', () => {
    const before = {
      id: 'r', label: '根', children: [
        { id: 'p', label: '父', children: [{ id: 'k', label: '子', children: [] }] },
        { id: 'q', label: '另一个父', children: [] },
      ],
    } as MindMapContent['root'];
    const after = {
      id: 'r', label: '根', children: [
        { id: 'p', label: '父', children: [] },
        { id: 'q', label: '另一个父', children: [{ id: 'k', label: '子', children: [] }] },
      ],
    } as MindMapContent['root'];

    const changes = diffCandidate(before, after)!;
    const moved = changes.find(item => item.kind === 'moved');
    expect(moved?.id).toBe('k');
    expect(moved?.detail).toContain('另一个父');
  });

  it('没有可比基线（create）返回 null，而不是"全部新增"', () => {
    expect(diffCandidate(null, VISUAL_MAP.root)).toBeNull();
  });

  it('两棵树一样时是空数组（不是 null）', () => {
    expect(diffCandidate(VISUAL_MAP.root, structuredClone(VISUAL_MAP.root))).toEqual([]);
  });
});

describe('op 执行器：视觉元素', () => {
  const base = (): MindMapContent => structuredClone(VISUAL_MAP);

  it('add 带上颜色与图标', () => {
    const result = applyOps(base(), {
      batch_id: 'b', base_revision: 0,
      ops: [{
        op: 'add', id: '$n', parent_id: 'r', index: 0, label: '新分支',
        color: 'green', icon: '✅',
      }],
    });
    const added = result.content.root.children[0];
    expect(added.color).toBe('green');
    expect(added.icon).toBe('✅');
  });

  it('update 能设也能清（空串 = 清空）', () => {
    const set = applyOps(base(), {
      batch_id: 'b', base_revision: 0,
      ops: [{ op: 'update', id: 'a', changes: { color: 'teal', icon: '📊' } }],
    });
    expect(set.content.root.children[0].color).toBe('teal');

    const cleared = applyOps(set.content, {
      batch_id: 'b2', base_revision: 1,
      ops: [{ op: 'update', id: 'a', changes: { color: '', icon: '' } }],
    });
    expect(cleared.content.root.children[0].color).toBeNull();
    expect(cleared.content.root.children[0].icon).toBeNull();
    // 标题不在这条路上
    expect(cleared.content.root.children[0].label).toBe('选题');
  });

  it('update 只给空串也算"改了一个字段"', () => {
    const result = applyOps(base(), {
      batch_id: 'b', base_revision: 0,
      ops: [{ op: 'update', id: 'b', changes: { color: '' } }],
    });
    expect(result.content.root.children[1].color).toBeNull();
  });

  it('非法颜色被拒，且整批不生效', () => {
    const content = base();
    expect(() => applyOps(content, {
      batch_id: 'b', base_revision: 0,
      ops: [{ op: 'update', id: 'a', changes: { color: '#2563eb' as never } }],
    })).toThrow(OpError);
    // 原子：原树没变
    expect(content.root.children[0].color).toBe('blue');
  });

  it('保存前校验会拦下非法颜色（不等到 422）', () => {
    const bad = base();
    (bad.root.children[0] as { color: string }).color = 'navy';
    expect(validateDraft(bad)).toContain('调色板');
  });
});
