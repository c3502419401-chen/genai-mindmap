import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import { REAL_TREES } from './fixtures/real-trees';
import type { MindMapContent, MindNode } from '../src/api/types';
import { MINDMAP_VISIBLE_NODE_LIMIT, countSubtree, viewTree } from '../src/canvas/view-collapse';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 一级分支上的**概括角标**：「还有 21 个 · 训练与并行」。
 *
 * 它补的是 `+N` 补不了的那一半（`docs/VIEW-COLLAPSE.md` §七）：一枚 `+3` 只能证明
 * "这里还有 3 个直接孩子"，证明不了"这一支一共还有 21 个、说的是什么"。这一组用例
 * 钉五件事：
 *
 * 1. **数字对不对**：角标上的数必须能从树里核出来（`countSubtree` − 画出来的），
 *    不是估的；
 * 2. **只在一级分支上**：画布上 6 枚，不多不少（136 节点那棵树就是 6 支）；
 * 3. **点一枚 = 整支冒出来**：数字是整支的，动作就得是整支的——点完角标消失；
 * 4. **没有概括就不画**：旧图 / 开关关着时生成的图里没有 `branch_digests`，
 *    一枚都不该出现（否则它只是在重复 `+N` 与顶部「显示 40 / 136」）；
 * 5. **全展开时一枚都没有**：那时没有"还有"。
 */

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class DOMMatrixReadOnlyStub {
  m11 = 1;
  m22 = 1;
  m41 = 0;
  m42 = 0;
}

const baseTree = REAL_TREES.tree136 as MindMapContent;
const BRANCHES = baseTree.root.children;

/** 给真树挂上一份概括（键就是要验的那 6 个一级分支 id）。 */
function withDigests(texts: Record<string, string>): MindMapContent {
  return { ...baseTree, branch_digests: texts };
}

/** 六条概括：形状与真模型产出的一致（8–12 字的名词短语）。 */
const DIGEST_TEXT: Record<string, string> = Object.fromEntries(
  BRANCHES.map((branch, index) => [branch.id, `第${index + 1}支讲的事`]));

/** 画布上真的画出来的概括角标：**两半都在**才算（与模板的 `v-if` 同一判据）。 */
function marksOf(store: ReturnType<typeof useMindMapStore>) {
  return store.nodes.filter(node => node.data.branchDigest && node.data.branchHidden);
}

const wrappers: VueWrapper[] = [];

function mountCanvas(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(MindMapCanvas, { attachTo: document.body, global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

/** 视图树里以 `id` 为根的那一支画出来了几个节点（含自身）。 */
function subtreeIn(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = subtreeIn(child, id);
    if (hit) return hit;
  }
  return null;
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('一级分支的概括角标：数字与文案', () => {
  it('收起态：136 节点那棵树上有 6 枚，数字等于"整支没画出来的节点数"', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests(DIGEST_TEXT), 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const marks = marksOf(store);
    expect(marks).toHaveLength(6);
    // 每一枚的数字都要能从树里核出来：整支节点数 − 这一屏画出来的这一支
    const view = viewTree(baseTree.root, { limit: MINDMAP_VISIBLE_NODE_LIMIT });
    for (const mark of marks) {
      const total = countSubtree(subtreeIn(baseTree.root, mark.id) ?? baseTree.root);
      const shown = subtreeIn(view.root, mark.id);
      expect(mark.data.branchHidden).toBe(total - (shown ? countSubtree(shown) : 0));
      expect(mark.data.branchDigest).toBe(DIGEST_TEXT[mark.id]);
    }
    // 全部画出来 + 没画出来 = 136（一个节点都没丢）
    const hiddenSum = marks.reduce((sum, mark) => sum + (mark.data.branchHidden ?? 0), 0);
    expect(store.nodes.length + hiddenSum).toBe(136);
  });

  it('角标上写的是「还有 N 个 · 概括」，两段都在', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests(DIGEST_TEXT), 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const marks = wrapper.findAll('[data-test="node-branch-digest"]');
    expect(marks).toHaveLength(6);
    const first = marks[0];
    const hidden = marksOf(store)[0]?.data.branchHidden;
    expect(first.text()).toContain(`还有 ${hidden} 个`);
    expect(first.text()).toContain(DIGEST_TEXT[BRANCHES[0].id]);
    // 与 `+N` 是两枚不同的角标，不是同一个东西换了位置
    expect(wrapper.findAll('[data-test="node-folded"]').length).toBeGreaterThan(0);
  });

  it('只挂在一级分支上：深层节点一枚都没有', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests(DIGEST_TEXT), 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    for (const node of store.nodes) {
      if (node.data.branchDigest) expect(node.data.depth).toBe(1);
    }
  });
});

describe('点一枚：整支冒出来', () => {
  it('点角标 → 那一支全部出现、角标消失、别的支一个不多', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests(DIGEST_TEXT), 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const before = store.nodes.length;
    const target = marksOf(store)[0];
    expect(target).toBeDefined();
    const hidden: number = target?.data.branchHidden ?? 0;
    expect(hidden).toBeGreaterThan(0);

    await wrapper.findAll('[data-test="node-branch-digest"]')[0].trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    // 数字兑现：点下去冒出来的个数 == 角标上的数
    expect(store.nodes.length).toBe(before + hidden);
    // 那一支的角标消失（这一支没有"还有"了），其余 5 枚还在
    const left = marksOf(store);
    expect(left.length).toBe(5);
    expect(left.some(node => node.id === target?.id)).toBe(false);
  });

  it('全展开时一枚都没有（那时没有"还有"）', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests(DIGEST_TEXT), 'right');
    await flushPromises();

    await wrapper.get('[data-test="view-expand-all"]').trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(store.nodes.length).toBe(136);
    expect(marksOf(store)).toHaveLength(0);
  });
});

describe('没有概括就不画', () => {
  it('旧图（内容里没有 branch_digests）：一枚都不出现', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(baseTree, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(wrapper.findAll('[data-test="node-branch-digest"]')).toHaveLength(0);
    // 而 `+N` 照旧在——收起这件事一个字都没变
    expect(wrapper.findAll('[data-test="node-folded"]').length).toBeGreaterThan(0);
  });

  it('概括指向一个不存在的 id：那一支不画，其余照画', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    store.preview(withDigests({ 'no-such-node': '幽灵概括' }), 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(wrapper.findAll('[data-test="node-branch-digest"]')).toHaveLength(0);
  });
});

describe('`unshown` 与 `folded` 不是同一个量', () => {
  it('真树上存在"孩子全画了、深处被截"的支：它在 `unshown` 里，不在 `folded` 里', () => {
    const view = viewTree(baseTree.root, { limit: MINDMAP_VISIBLE_NODE_LIMIT });
    const withGap = BRANCHES.filter(branch => view.unshown.has(branch.id));
    const selfCut = BRANCHES.filter(branch => view.folded.has(branch.id));
    // 六支都有没画出来的节点（否则角标会凭空少几枚）
    expect(withGap).toHaveLength(6);
    // 而"这个父节点自己有直接孩子没画"的支只是其中一部分——两者一旦混用，
    // 那些支的角标会消失，用户看不出为什么
    expect(selfCut.length).toBeLessThan(withGap.length);
  });
});

describe('差得太少的支不挂角标', () => {
  it('一支只差 2 个节点：不画（读一句概括比直接点开更贵）', async () => {
    const wrapper = mountCanvas();
    const store = useMindMapStore();
    // root + 两支：A 有 8 个孩子（差 4），B 有 2 个（差 2）
    const content = {
      title: '小树',
      root: {
        id: 'root', label: '小树', children: [
          { id: 'A', label: '甲', children: [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ id: `a${i}`, label: `甲${i}`, children: [] })) },
          { id: 'B', label: '乙', children: [1, 2].map(i => ({ id: `b${i}`, label: `乙${i}`, children: [] })) },
        ],
      },
      branch_digests: { A: '甲的概括', B: '乙的概括' },
    } as unknown as MindMapContent;
    // 上限 7 = 根 + A/B 两支 + A 的 4 个孩子：A 差 4（过线），B 的两个孩子都没画（差 2）
    store.visibleNodeLimit = 7;
    store.preview(content, 'right');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const marks = marksOf(store);
    expect(marks).toHaveLength(1);
    expect(marks[0].id).toBe('A');
    expect(marks[0].data.branchHidden).toBe(4);
  });
});
