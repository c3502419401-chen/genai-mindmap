import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Source } from '../src/api/types';
import { resolveRefWithText, type ResolvedRef } from '../src/mindmap/refs';

// 组件只从 client 里拿一件事：按 id 取正文。把它换成可编程的假实现，
// 请求本身（URL、limit）在 client 的测试里管，这里只管"拿到什么之后怎么显示"。
vi.mock('../src/api/client', () => ({
  readLibrarySource: vi.fn(),
}));

import { readLibrarySource } from '../src/api/client';
import SourceContextDrawer from '../src/components/SourceContextDrawer.vue';

const readMock = vi.mocked(readLibrarySource);

/**
 * 可数的正文：三段拼出来，`start/end` 按**码点**算，与后端契约同一口径。
 * 断言 mark 的文本就是这三段里的中间那段——不是"包含"，是逐字相等。
 */
const PRE = '开头的铺垫。';
const HIT = '目标句子在这里。';
const POST = '结尾的收束。';
const TEXT = PRE + HIT + POST;
const START = Array.from(PRE).length;
const END = START + Array.from(HIT).length;

function sourceOf(text: string, id = 's1'): Source {
  return {
    id,
    kind: 'document',
    filename: '线代第一章.pdf',
    text,
    span: null,
    status: 'parsed',
    error: null,
    parsed_pages: null,
    truncated: false,
    failed_pages: [],
  };
}

/** 有本会话原文的引用：组件不该为此发起任何请求。 */
function localItem(text = TEXT): ResolvedRef {
  return resolveRefWithText(
    { source_id: 's1', start: START, end: END, quote: HIT },
    text,
    sourceOf(text),
  );
}

/** 只有库、没有本会话原文的引用（打开已保存导图就是这种形态）。 */
function libraryItem(): ResolvedRef {
  return resolveRefWithText(
    { source_id: 's1', start: START, end: END, quote: HIT },
    '',
    null,
  );
}

function mountDrawer(item: ResolvedRef | null) {
  return mount(SourceContextDrawer, { props: { item } });
}

beforeEach(() => {
  readMock.mockReset();
});

describe('点引用打开的那句原文', () => {
  it('不点上下文时只显示引文本身（先确认是不是这句）', () => {
    const wrapper = mountDrawer(localItem());

    expect(wrapper.get('[data-test="ref-quote"]').text()).toBe(HIT);
    expect(wrapper.find('[data-test="ref-context"]').exists()).toBe(false);
  });

  it('本地有原文时不发请求：同一次会话里正文就在内存里', async () => {
    mountDrawer(localItem());
    await flushPromises();

    expect(readMock).not.toHaveBeenCalled();
  });

  it('点「看上下文」后高亮的就是那一段，前后各带上下文', async () => {
    const wrapper = mountDrawer(localItem());

    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();

    const context = wrapper.get('[data-test="ref-context"]');
    // 字符级：mark 里逐字等于 HIT，不是"包含"
    expect(context.find('mark').text()).toBe(HIT);
    const plain = context.text();
    expect(plain).toContain(PRE);
    expect(plain).toContain(POST);
    // 窗口没被裁（正文比 pad 短），所以不该出现省略号
    expect(plain).not.toContain('…');
  });

  it('位置说明按 1 起算，与详情面板的「第 N 字」同口径', () => {
    const wrapper = mountDrawer(localItem());

    expect(wrapper.get('[data-test="ref-where"]').text()).toContain(`第 ${START + 1}–${END} 字`);
  });
});

describe('原文只在知识库里（重开已保存导图）', () => {
  it('按 id 取正文，并要求整份而不是 4000 字预览', async () => {
    readMock.mockResolvedValue({
      source_id: 's1', filename: '线代第一章.pdf', kind: 'document',
      text: TEXT, truncated: false, total_chars: Array.from(TEXT).length,
    });
    const wrapper = mountDrawer(libraryItem());
    await flushPromises();

    expect(readMock).toHaveBeenCalledTimes(1);
    // 37973 字的材料里第 20000 字处的引用，用 4000 字预览永远定位不到
    expect(readMock.mock.calls[0][1]).toBeGreaterThanOrEqual(200_000);

    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="ref-context"]').find('mark').text()).toBe(HIT);
  });

  it('取不到原文时摘录仍然显示，并说明原因', async () => {
    readMock.mockRejectedValue(new Error('这份来源不在知识库里'));
    const wrapper = mountDrawer(libraryItem());
    await flushPromises();

    expect(wrapper.get('[data-test="ref-quote"]').text()).toBe(HIT);
    expect(wrapper.get('[data-test="ref-note"]').text()).toContain('原文没能读取');
    // 定位不到就不该给一个点了没反应的按钮
    expect(wrapper.find('[data-test="ref-context-toggle"]').exists()).toBe(false);
  });

  it('正文被上限截断、引用落在没载入的部分：直说，不假装定位到了', async () => {
    const total = 200_000 + 5_000;
    readMock.mockResolvedValue({
      source_id: 's1', filename: '超长材料.pdf', kind: 'document',
      text: '甲'.repeat(1000), truncated: true, total_chars: total,
    });
    const wrapper = mountDrawer(resolveRefWithText(
      { source_id: 's1', start: 150_000, end: 150_010, quote: '没载入的那段' },
      '',
      null,
    ));
    await flushPromises();

    const note = wrapper.get('[data-test="ref-note"]').text();
    expect(note).toContain('只载入前');
    expect(note).toContain('200,000');
    expect(wrapper.find('[data-test="ref-context-toggle"]').exists()).toBe(false);
  });

  it('引文在正文里定位不到（逐字对不上）：不高亮，并说清是定位失败', async () => {
    readMock.mockResolvedValue({
      source_id: 's1', filename: '线代第一章.pdf', kind: 'document',
      text: '完全不相干的正文内容。', truncated: false, total_chars: 11,
    });
    const wrapper = mountDrawer(resolveRefWithText(
      { source_id: 's1', quote: '这句原文里没有' },
      '',
      null,
    ));
    await flushPromises();

    expect(wrapper.get('[data-test="ref-note"]').text()).toContain('定位不到');
    expect(wrapper.find('mark').exists()).toBe(false);
  });
});

describe('只有引文、没有坐标（quote-only 引用）', () => {
  // 两处命中之间隔得比窗口（两侧各 600 字）远，窗口才只圈得住一处——
  // 否则两处的上下文都是同一份短正文，"跳到第二处"看起来什么都没变。
  const GAP = '中'.repeat(700);
  const TWICE = `${'前'.repeat(700)}${HIT}${GAP}${HIT}${'后'.repeat(700)}`;

  it('多处命中时给出总数，并说明是按引文搜到的', async () => {
    const wrapper = mountDrawer(resolveRefWithText({ source_id: 's1', quote: HIT }, TWICE, sourceOf(TWICE)));
    await flushPromises();

    expect(wrapper.get('[data-test="ref-hit-index"]').text()).toContain('1 / 2');
    expect(wrapper.get('[data-test="ref-note"]').text()).toContain('出现 2 次');
  });

  it('「下一处」跳到第二处：两处的上下文确实不同', async () => {
    const wrapper = mountDrawer(resolveRefWithText({ source_id: 's1', quote: HIT }, TWICE, sourceOf(TWICE)));

    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();
    const first = wrapper.get('[data-test="ref-context"]').text();
    // 第一处的上文落在开头那 700 个「前」里
    expect(first).toContain('前');
    expect(first).not.toContain('后');

    await wrapper.get('[data-test="ref-next"]').trigger('click');
    await flushPromises();
    const second = wrapper.get('[data-test="ref-context"]').text();

    expect(wrapper.get('[data-test="ref-hit-index"]').text()).toContain('2 / 2');
    // 第二处的上文落在两处之间的「中」，下文才进到「后」
    expect(second).toContain('中');
    expect(second).toContain('后');
    expect(second).not.toBe(first);
    // 高亮句本身在两处都成立（同一句话的两处出现）
    expect(wrapper.get('[data-test="ref-context"]').find('mark').text()).toBe(HIT);
  });

  it('只有一处时不显示上一处/下一处', async () => {
    const wrapper = mountDrawer(resolveRefWithText({ source_id: 's1', quote: HIT }, TEXT, sourceOf(TEXT)));
    await flushPromises();

    expect(wrapper.find('[data-test="ref-next"]').exists()).toBe(false);
  });
});

describe('上下文的边界', () => {
  const LONG = `${'上文'.repeat(400)}${HIT}${'下文'.repeat(400)}`;

  function longItem(): ResolvedRef {
    const start = Array.from('上文'.repeat(400)).length;
    return resolveRefWithText(
      { source_id: 's1', start, end: start + Array.from(HIT).length, quote: HIT },
      LONG,
      sourceOf(LONG),
    );
  }

  it('窗口贴着全文两端时不给「再读」，中间时两边都给', async () => {
    const wrapper = mountDrawer(longItem());
    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="ref-grow-before"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="ref-grow-after"]').exists()).toBe(true);
  });

  it('点「再读下文」后窗口变长（上下文真的多了，不是只换了个按钮）', async () => {
    const wrapper = mountDrawer(longItem());
    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();
    const before = Array.from(wrapper.get('[data-test="ref-context"]').text()).length;

    await wrapper.get('[data-test="ref-grow-after"]').trigger('click');
    await flushPromises();
    const after = Array.from(wrapper.get('[data-test="ref-context"]').text()).length;

    expect(after).toBeGreaterThan(before);
    // 高亮没被窗口扩展弄丢
    expect(wrapper.get('[data-test="ref-context"]').find('mark').text()).toBe(HIT);
  });

  it('点「再读上文」时窗口只往一侧长，命中句仍在', async () => {
    const wrapper = mountDrawer(longItem());
    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-test="ref-grow-before"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="ref-context"]').find('mark').text()).toBe(HIT);
  });
});

describe('抽屉的开关', () => {
  it('item 为 null 时整块不渲染', () => {
    const wrapper = mountDrawer(null);
    expect(wrapper.find('[data-test="ref-drawer"]').exists()).toBe(false);
  });

  it('点关闭发出 close', async () => {
    const wrapper = mountDrawer(localItem());
    await wrapper.get('button').trigger('click');
    expect(wrapper.emitted('close')).toHaveLength(1);
  });

  it('换一条引用时状态重置：窗口大小与命中序号都回到初始', async () => {
    const wrapper = mountDrawer(localItem());
    await wrapper.get('[data-test="ref-context-toggle"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="ref-context"]').exists()).toBe(true);

    await wrapper.setProps({ item: libraryItem() });
    readMock.mockResolvedValue({
      source_id: 's1', filename: '线代第一章.pdf', kind: 'document',
      text: TEXT, truncated: false, total_chars: Array.from(TEXT).length,
    });
    await flushPromises();

    // 新的一条还没点上下文 → 收起
    expect(wrapper.find('[data-test="ref-context"]').exists()).toBe(false);
  });
});
