import { describe, expect, it } from 'vitest';

import type { MindNode, Source, SourceUsage } from '../src/api/types';
import { buildRefIndex, excerptOf, locateInText, resolveRef, windowOf } from '../src/mindmap/refs';

/**
 * 「点引用跳回原文」这件事，**数据层与纯函数层早就够了，界面没接上**。
 *
 * 这份用例不改任何实现，只把现状钉下来：哪些能力已经现成可用、缺的那一环长什么样。
 * 下一条提交把缺口补上时，会连带改掉这里被标成「缺口」的那两条断言——它们记录的
 * 正是要修的行为。
 */

const TEXT = '矩阵的秩等于行向量组的极大线性无关组的个数。初等变换不改变矩阵的秩。';

function source(text: string | null = TEXT): Source {
  return {
    id: 's1', kind: 'document', filename: '线代第一章.pdf', text,
    span: null, status: 'parsed', error: null, parsed_pages: null,
    truncated: false, failed_pages: [],
  };
}

const usage: SourceUsage[] = [
  { source_id: 's1', kind: 'document', filename: '线代第一章.pdf', char_count: TEXT.length },
];

describe('已经有的能力（数据层给得出手）', () => {
  it('有坐标就能定位，且区间按码点算', () => {
    expect(locateInText(TEXT, { source_id: 's1', start: 0, end: 6 })).toEqual({ start: 0, end: 6 });
  });

  it('没有坐标时能按引文搜到', () => {
    const found = locateInText(TEXT, { source_id: 's1', quote: '初等变换' });
    expect(found).not.toBeNull();
    expect(Array.from(TEXT).slice(found!.start, found!.end).join('')).toBe('初等变换');
  });

  it('能切出命中前后各一段上下文（高亮的前提）', () => {
    const { before, hit, after } = windowOf(TEXT, 0, 6, 4);
    expect(hit).toBe('矩阵的秩等于');
    expect(before).toBe('');
    expect(after).toContain('行向量组');
  });

  it('摘录能按 pad 裁，两端加省略号', () => {
    const text = excerptOf('甲'.repeat(300), 150, 160, 10);
    expect(text.startsWith('…')).toBe(true);
    expect(text.endsWith('…')).toBe(true);
  });

  it('反向索引：从材料看被哪些节点引用（SourceInputs 那一侧在用）', () => {
    const tree: MindNode = {
      id: 'r', label: '根',
      children: [{ id: 'a', label: '甲', children: [], source_refs: [{ source_id: 's1', quote: '甲' }] }],
    };
    expect(buildRefIndex(tree).counts.s1).toBe(1);
  });

  it('本次会话解析过的来源：摘录与三段切分都算得出来', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source()], usage);
    expect(resolved.hit).toBe('矩阵的秩等于');
    expect(resolved.start).toBe(0);
  });
});

describe('缺的那一环', () => {
  // 缺口一：正文不随导图保存。重开一张已保存的图时 `readySources` 是空的，
  // `resolveRef` 拿不到 `Source`，于是 start/end 为 null、excerpt 为空——
  // 界面上只剩下 `quote` 这一句，**没有任何办法回到原文那一段**。
  // 要补的正是这条：按 `source_id` 去知识库把正文取回来。
  it('没有本次会话的 Source 时，定位信息全丢（这就是要补的那一环）', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [], usage);
    expect(resolved.source).toBeNull();
    expect(resolved.start).toBeNull();
    expect(resolved.excerpt).toBe('');
    // 引文本身还在（后端校验过的），但只有它
    expect(resolved.quote).toBe('矩阵的秩等于');
  });

  it('正文被清空时同样定位不到', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source(null)], usage);
    expect(resolved.start).toBeNull();
  });
});
