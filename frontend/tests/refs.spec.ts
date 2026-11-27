import { describe, expect, it } from 'vitest';

import type { MindNode, Source, SourceRef, SourceUsage } from '../src/api/types';
import { buildRefIndex, excerptOf, locateAll, locateInText, quoteOffsets, refCoverage, refName, refWhere, resolveRef, resolveRefs, resolveRefWithText, windowOf } from '../src/mindmap/refs';

const TEXT = '矩阵的秩等于行向量组的极大线性无关组的个数。初等变换不改变矩阵的秩。';

function source(id = 's1', text: string | null = TEXT, kind: Source['kind'] = 'document'): Source {
  return {
    id,
    kind,
    filename: kind === 'document' ? '线代第一章.pdf' : null,
    text,
    span: null,
    status: 'parsed',
    error: null,
    parsed_pages: null,
    truncated: false,
    failed_pages: [],
  };
}

const usage: SourceUsage[] = [
  { source_id: 's1', kind: 'document', filename: '线代第一章.pdf', char_count: TEXT.length },
];

describe('定位引用', () => {
  it('有字符区间时直接采用（区间按码点算）', () => {
    const ref: SourceRef = { source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' };
    expect(locateInText(TEXT, ref)).toEqual({ start: 0, end: 6 });
  });

  it('区间越界时退回到按引文查找', () => {
    const ref: SourceRef = { source_id: 's1', start: 999, end: 1000, quote: '初等变换' };
    expect(locateInText(TEXT, ref)).toEqual({ start: 22, end: 26 });
  });

  it('没有区间也没有引文时定位不到', () => {
    expect(locateInText(TEXT, { source_id: 's1' })).toBeNull();
  });

  it('引文找不到时定位不到，而不是给个假位置', () => {
    expect(locateInText(TEXT, { source_id: 's1', quote: '这句话原文里没有' })).toBeNull();
  });

  it('码点与 UTF-16 下标不一致时按码点切，不会把 emoji 切坏', () => {
    const emoji = '😀😀结论：秩相等';
    const ref: SourceRef = { source_id: 's2', quote: '结论' };
    // 😀 占两个 UTF-16 单元，码点下标是 2 而不是 4
    expect(locateInText(emoji, ref)).toEqual({ start: 2, end: 4 });
  });

  it('空白被压过的引文仍能定位', () => {
    const text = '第一行\n\n  第二行 有关键词';
    const ref: SourceRef = { source_id: 's3', quote: '第二行 有关键词' };
    expect(locateInText(text, ref)).not.toBeNull();
  });
});

describe('位置说明与来源名', () => {
  it('页码与字符区间同时存在时都写', () => {
    expect(refWhere({ source_id: 's1', page: 3, start: 119, end: 168 })).toBe('第 3 页 · 第 120–168 字');
  });

  it('只有页码', () => {
    expect(refWhere({ source_id: 's1', page: 5 })).toBe('第 5 页');
  });

  it('两项都没有时返回空串', () => {
    expect(refWhere({ source_id: 's1' })).toBe('');
  });

  it('来源名优先用文件名，缺失时退回类型名', () => {
    expect(refName({ source_id: 's1' }, usage)).toBe('文档 · 线代第一章.pdf');
    expect(refName({ source_id: 's9' }, [])).toBe('来源 s9');
  });
});

describe('解析成可绘制形状', () => {
  it('有原文时给出摘录与高亮区间', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source()], usage);
    expect(resolved.source).not.toBeNull();
    expect(resolved.quote).toBe('矩阵的秩等于');
    expect(resolved.start).toBe(0);
    expect(resolved.end).toBe(6);
    expect(resolved.excerpt).toContain('矩阵的秩等于');
    expect(resolved.where).toBe('第 1–6 字');
  });

  it('没有原文（打开已保存导图）时仍显示摘录，但不给高亮区间', () => {
    const resolved = resolveRef({ source_id: 's1', page: 2, quote: '初等变换' }, [], usage);
    expect(resolved.source).toBeNull();
    expect(resolved.quote).toBe('初等变换');
    expect(resolved.excerpt).toBe('');
    expect(resolved.start).toBeNull();
    expect(resolved.where).toBe('第 2 页');
  });

  it('没有 quote 但有字符区间时，从原文切出摘录', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 4 }, [source()], usage);
    expect(resolved.quote).toBe('矩阵的秩');
  });

  it('原文和摘录都没有时返回空串，不抛错', () => {
    const resolved = resolveRef({ source_id: 's1' }, [source('s1', null)], usage);
    expect(resolved.quote).toBe('');
    expect(resolved.excerpt).toBe('');
  });

  it('resolveRefs 接受 undefined 并保持顺序', () => {
    expect(resolveRefs(undefined, [], [])).toEqual([]);
    const list = resolveRefs(
      [{ source_id: 's1', quote: '甲' }, { source_id: 's1', quote: '乙' }],
      [source()],
      usage,
    );
    expect(list.map(item => item.quote)).toEqual(['甲', '乙']);
  });

  it('片段截断处加省略号', () => {
    const long = '甲'.repeat(300);
    const text = excerptOf(long, 150, 160, 10);
    expect(text.startsWith('…')).toBe(true);
    expect(text.endsWith('…')).toBe(true);
    expect(Array.from(text).length).toBe(32);
  });
});

describe('原文片段的三段切分', () => {
  it('命中部分单独成段，前后各自带上下文', () => {
    const { before, hit, after } = windowOf(TEXT, 22, 26, 4);
    expect(hit).toBe('初等变换');
    expect(before).toBe('…的个数。');
    expect(after).toBe('不改变矩…');
  });

  it('三段拼起来等于整段摘录，不会有字被吃掉', () => {
    const resolved = resolveRef({ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' }, [source()], usage);
    expect(`${resolved.before}${resolved.hit}${resolved.after}`).toBe(resolved.excerpt);
    expect(resolved.hit).toBe('矩阵的秩等于');
  });
});
describe('来源反向索引', () => {
  const tree = (): MindNode => ({
    id: 'root',
    label: '线代',
    children: [
      {
        id: 'a',
        label: '矩阵的秩',
        source_refs: [{ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等' }],
        children: [
          {
            id: 'a1',
            label: '极大无关组',
            source_refs: [
              { source_id: 's1', start: 6, end: 20 },
              // 同一节点重复引用同一来源：只算一次
              { source_id: 's1', start: 6, end: 20 },
            ],
            children: [],
          },
        ],
      },
      { id: 'b', label: '条件数', source_refs: [{ source_id: 's2', quote: '条件数' }], children: [] },
      { id: 'c', label: '没引用', children: [] },
    ],
  });

  it('按来源统计节点数，同节点重复引用只算一次', () => {
    const index = buildRefIndex(tree());
    expect(index.counts.s1).toBe(2);
    expect(index.counts.s2).toBe(1);
    expect(index.counts.s3).toBeUndefined();
  });

  it('记录引用它的节点与路径，按先序排列', () => {
    const index = buildRefIndex(tree());
    expect(index.hits.s1.map(hit => hit.id)).toEqual(['a', 'a1']);
    expect(index.hits.s1[1].path).toEqual(['线代', '矩阵的秩', '极大无关组']);
    expect(index.hits.s2[0].path).toEqual(['线代', '条件数']);
  });

  it('没有导图时返回空索引，调用方不必先判空', () => {
    const index = buildRefIndex(null);
    expect(index.counts).toEqual({});
    expect(index.hits).toEqual({});
  });
});

/**
 * "点引用跳原文"依赖的定位：**同一句引文可能出现多次**，而 `locateInText`
 * 只回答"第一处在哪"——那会让界面笃定地指向一个没有依据的位置。
 */
describe('定位的全部候选（locateAll）', () => {
  const LONG = '开头一段话。目标句子在这里。中间隔了一些字。目标句子在这里。结尾。';

  it('有坐标时直接采用，不去猜别处', () => {
    const found = locateAll('矩阵的秩等于行向量组的极大线性无关组的个数。', {
      source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于',
    });
    expect(found).toEqual({ start: 0, end: 6, others: [], via: 'range' });
  });

  it('坐标越界时退回到按引文搜，并给出**其余**位置', () => {
    const found = locateAll(LONG, { source_id: 's1', start: 0, end: 999, quote: '目标句子在这里' });
    const first = LONG.indexOf('目标句子在这里');
    const second = LONG.indexOf('目标句子在这里', first + 1);

    expect(found.via).toBe('quote');
    expect(found.start).toBe(Array.from(LONG.slice(0, first)).length);
    expect(found.others).toEqual([Array.from(LONG.slice(0, second)).length]);
  });

  it('只有一处命中时 others 为空', () => {
    const found = locateAll(LONG, { source_id: 's1', quote: '中间隔了一些字' });
    expect(found.via).toBe('quote');
    expect(found.others).toEqual([]);
    expect(LONG.startsWith('中间', found.start) || Array.from(LONG).slice(found.start, found.start + 2).join('') === '中间').toBe(true);
  });

  it('引文被换行/空格改写过：按空白归一后仍能定位，但只算"搜到"不算精确坐标', () => {
    const text = '第一句。第二句\n  换行之后的内容。第三句。';
    const found = locateAll(text, { source_id: 's1', quote: '第二句 换行之后的内容' });
    expect(found.via).toBe('quote');
    expect(found.start).toBeGreaterThan(0);
  });

  it('定位不到时 via 是 none，且不带一个假坐标', () => {
    const found = locateAll('很短的一段', { source_id: 's1', quote: '原文里没有这句' });
    expect(found).toEqual({ start: 0, end: 0, others: [], via: 'none' });
  });

  it('空正文 / 空引用都不抛错', () => {
    expect(locateAll('', { source_id: 's1', quote: '甲' }).via).toBe('none');
    expect(locateAll('甲乙丙', { source_id: 's1' }).via).toBe('none');
  });

  it('重叠命中各算一处（与后端 _quote_occurrences 同口径）', () => {
    expect(quoteOffsets('aaaa', 'aa')).toEqual([0, 1, 2]);
    expect(quoteOffsets('甲乙丙', '丁')).toEqual([]);
  });

  it('码点下标：emoji 与汉字混排时不会切在半个字符上', () => {
    const text = '🎯🎯矩阵的秩';
    const found = locateAll(text, { source_id: 's1', quote: '矩阵的秩' });
    expect(found.start).toBe(2);
    expect(Array.from(text).slice(found.start, found.end).join('')).toBe('矩阵的秩');
  });

  // 实测踩出来的：引用片段以空格/换行开头时（英文论文段落很常见），
  // 旧实现拿 `quote.trim()` 与原文区间比较 → 判成"坐标对不上" → 退回全文搜索 →
  // 高亮少一个字、位置说明偏 1、还多出一句"这条引用没有坐标"。
  it('引文带前导空白时坐标仍然可信，不退回搜索', () => {
    const text = ' 甲乙丙丁戊';
    const found = locateAll(text, { source_id: 's1', start: 0, end: 3, quote: ' 甲乙' });
    expect(found).toEqual({ start: 0, end: 3, others: [], via: 'range' });

    // 引文被 trim 过（只剩正文）也要认：首尾空白不改变"指的是哪一段"
    const trimmed = locateAll(text, { source_id: 's1', start: 0, end: 3, quote: '甲乙' });
    expect(trimmed.via).toBe('range');
    expect(trimmed.start).toBe(0);
    expect(trimmed.end).toBe(3);
  });

  it('引文带尾随空白时同样不误判', () => {
    const text = '甲乙丙\n\n下一段';
    const found = locateAll(text, { source_id: 's1', start: 0, end: 4, quote: '甲乙丙' });
    expect(found.via).toBe('range');
    expect(found.end).toBe(4);
  });

  it('坐标真的指向别的内容时才退回搜索（不是所有不等都放过）', () => {
    const text = '前文。目标是甲。后文。';
    const found = locateAll(text, { source_id: 's1', start: 0, end: 3, quote: '目标是甲' });
    expect(found.via).toBe('quote');
    expect(Array.from(text).slice(found.start, found.end).join('')).toBe('目标是甲');
  });
});

/**
 * `resolveRefWithText`：正文不在 `sources` 数组里（从知识库取回来的那份）时，
 * 也要能算出高亮。这是"打开已保存导图之后还能跳回原文"的唯一通道。
 */
describe('用外部原文解析引用', () => {
  const TEXT2 = '矩阵的秩等于行向量组的极大线性无关组的个数。初等变换不改变矩阵的秩。';

  it('正文来自参数而不是 sources，位置按码点切', () => {
    const resolved = resolveRefWithText(
      { source_id: 's1', start: 0, end: 6, quote: '矩阵的秩等于' },
      TEXT2,
      null,
      usage,
    );
    expect(resolved.hit).toBe('矩阵的秩等于');
    expect(resolved.start).toBe(0);
    expect(resolved.end).toBe(6);
    // 名字仍然能取到：来自 usage，不依赖 Source
    expect(resolved.name).toContain('线代第一章');
  });

  it('定位不到时 start/end 为 null，摘录为空（界面据此不高亮）', () => {
    const resolved = resolveRefWithText(
      { source_id: 's1', quote: '这句原文里没有' },
      TEXT2,
      null,
      usage,
    );
    expect(resolved.start).toBeNull();
    expect(resolved.excerpt).toBe('');
    // 但引文本身还在：不会因为定位失败就丢
    expect(resolved.quote).toBe('这句原文里没有');
  });

  it('resolveRef 与它同源：本地有原文时两者结果一致', () => {
    const ref: SourceRef = { source_id: 's1', start: 6, end: 16, quote: '行向量组的极大' };
    const viaSource = resolveRef(ref, [source('s1', TEXT2)], usage);
    const viaText = resolveRefWithText(ref, TEXT2, null, usage);
    expect(viaText.hit).toBe(viaSource.hit);
    expect(viaText.excerpt).toBe(viaSource.excerpt);
  });
});

/**
 * 覆盖率按来历拆数。
 *
 * 这个分组是 `Node.origin` 存在的**全部**理由：同一个"12 个没有引用"里，
 * `generated` 那些可以补材料再生成一次，`manual` 那些本来就没有原文可引。
 * 合成一个数时界面只能把它们并列成一句免责声明。
 */
describe('引用覆盖率按来历分组', () => {
  const node = (id: string, extra: Partial<MindNode> = {}): MindNode =>
    ({ id, label: id, children: [], ...extra });

  it('没引用的节点按 origin 分开数，缺字段算 unknown', () => {
    const root = node('r', {
      origin: 'generated',
      children: [
        node('cited', { origin: 'generated', source_refs: [{ source_id: 's1' }] }),
        node('gen', { origin: 'generated' }),
        node('manual', { origin: 'manual' }),
        node('old'),                                    // 旧图：没有这个字段
      ],
    });

    const stat = refCoverage(root);
    expect(stat.total).toBe(5);
    expect(stat.cited).toBe(1);
    // 有引用的那个不计入任何一组；根自己（没有引用）算进 generated
    expect(stat.uncited).toEqual({ generated: 2, manual: 1, unknown: 1 });
  });

  it('一组都不缺（三种都可能是 0），界面才不用判 undefined', () => {
    expect(refCoverage(null).uncited).toEqual({ generated: 0, manual: 0, unknown: 0 });
    const stat = refCoverage(node('r', { origin: 'manual' }));
    expect(stat.uncited).toEqual({ generated: 0, manual: 1, unknown: 0 });
  });
});