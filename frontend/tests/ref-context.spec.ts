import { describe, expect, it } from 'vitest';

import {
  CONTEXT_PAD,
  CONTEXT_STEP,
  beyondLoaded,
  contextSlice,
  cycleIndex,
  growPad,
} from '../src/mindmap/context-window';

/**
 * 窗口这一层全是边界算术，错了的表现都很安静：多留一段不报错、少留一段不报错、
 * "再读下文"点了没反应也不报错。所以这里按**边界**而不是按"正常情况"写。
 */

/** 造一段可数的正文：`长` 字，第 i 个字是 `(i % 10)`，便于断言"切的是哪一段"。 */
function numbered(length: number): string {
  return Array.from({ length }, (_, i) => String(i % 10)).join('');
}

describe('contextSlice', () => {
  it('命中在正中时，前后各留 pad', () => {
    const text = numbered(2000);
    const slice = contextSlice(text, 1000, 1010, 2000, 100);

    expect(slice.from).toBe(900);
    expect(slice.to).toBe(1110);
    expect(slice.hit).toBe('0123456789');
    expect(slice.moreBefore).toBe(true);
    expect(slice.moreAfter).toBe(true);
  });

  it('命中贴着开头：没有上文，也不给"再读上文"', () => {
    const text = numbered(500);
    const slice = contextSlice(text, 0, 5, 500, 100);

    expect(slice.from).toBe(0);
    expect(slice.before).toBe('');
    expect(slice.moreBefore).toBe(false);
    expect(slice.hit).toBe('01234');
  });

  it('命中贴着结尾：没有下文', () => {
    const text = numbered(500);
    const slice = contextSlice(text, 495, 500, 500, 100);

    expect(slice.to).toBe(500);
    expect(slice.after).toBe('');
    expect(slice.moreAfter).toBe(false);
  });

  // 这是"点引用跳原文"的核心：37973 字的材料里第 30000 字处的一条引用，
  // 显示的必须是那一段，而不是整份材料（那等于没定位）。
  it('长材料只切一段：窗口大小与全文长度无关', () => {
    const text = numbered(37973);
    const slice = contextSlice(text, 30000, 30020, 37973, 600);

    expect(slice.from).toBe(29400);
    expect(slice.to).toBe(30620);
    expect(slice.hit).toHaveLength(20);
    // 上下都还有，所以两侧都能再读
    expect(slice.moreBefore).toBe(true);
    expect(slice.moreAfter).toBe(true);
    // 窗口里的字数是 pad + 命中 + pad，不是 37973
    expect(Array.from(slice.before).length).toBe(600 + 1);
    expect(Array.from(slice.after).length).toBe(600 + 1);
  });

  it('正文被截断时，下文按**完整长度**判（否则会说"到底了"其实只是没载入）', () => {
    const loaded = numbered(1000);
    const slice = contextSlice(loaded, 900, 910, 37973, 50);

    // 窗口只能切到手里这份正文的末尾
    expect(slice.to).toBe(960);
    // 但"下面还有没有"按完整长度判：37973 > 960
    expect(slice.moreAfter).toBe(true);
  });

  it('未给 total 时退回手里这份长度（同一次会话里本地来源就是全文）', () => {
    const text = numbered(200);
    const slice = contextSlice(text, 0, 5, Array.from(text).length, 100);

    expect(slice.moreBefore).toBe(false);
    expect(slice.moreAfter).toBe(true);
  });
});

describe('growPad', () => {
  it('一次加一档', () => {
    expect(growPad(CONTEXT_PAD, 'after', 100, 120, 100_000)).toBe(CONTEXT_PAD + CONTEXT_STEP);
    expect(growPad(CONTEXT_PAD, 'before', 5000, 5020, 100_000)).toBe(CONTEXT_PAD + CONTEXT_STEP);
  });

  it('剩余不足一档时一次给完，不多留', () => {
    // 命中在第 1000 字处，上文只剩 1000 字，而一档是 1800 → pad 停在 1000
    expect(growPad(CONTEXT_PAD, 'before', 1000, 1020, 100_000)).toBe(1000);
  });

  it('那一侧已经全在窗口里 → 原值返回（调用方据此不给按钮）', () => {
    expect(growPad(600, 'before', 300, 320, 100_000)).toBe(600);
    expect(growPad(600, 'after', 100, 120, 500)).toBe(600);
  });
});

describe('cycleIndex', () => {
  it('环形轮换', () => {
    expect(cycleIndex(0, 1, 3)).toBe(1);
    expect(cycleIndex(2, 1, 3)).toBe(0);
    expect(cycleIndex(0, -1, 3)).toBe(2);
  });

  it('只有一处时永远是 0', () => {
    expect(cycleIndex(0, 1, 1)).toBe(0);
    expect(cycleIndex(0, -1, 0)).toBe(0);
  });
});

describe('beyondLoaded', () => {
  it('引用落在没载入的部分', () => {
    expect(beyondLoaded(150_000, 100_000, 200_000)).toBe(true);
  });

  it('正文是完整的 → 永远不越界', () => {
    expect(beyondLoaded(37_000, 37_973, 37_973)).toBe(false);
  });

  it('引用在已载入范围内', () => {
    expect(beyondLoaded(50_000, 100_000, 200_000)).toBe(false);
  });
});
