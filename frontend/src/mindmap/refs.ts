/**
 * 节点引用的展示逻辑。纯函数，不碰任何 store：定位、摘录、命名都在这里算完，
 * 组件只负责摆位置。
 *
 * 两个前提决定了这里必须做降级：
 * 1. 原文不随导图保存（`source_usage` 只记了 kind / filename / char_count），
 *    从"已保存的导图"打开时手里只有 `quote`，没有 `Source.text`。
 * 2. 字符区间按 Unicode 码点算（后端契约如此），不是 UTF-16 下标；
 *    直接拿 `String.slice` 切会把 emoji 和部分汉字切在半个上。
 */
import type { MindNode, NodeOrigin, Source, SourceRef, SourceUsage } from '../api/types';
import { SOURCE_LABELS } from '../sources/input';

export interface ResolvedRef {
  ref: SourceRef;
  /** 本次会话里同一份来源的原文；从已保存导图打开时为 null。 */
  source: Source | null;
  /** 摘录：`quote` 优先，没有就从原文按字符区间切。 */
  quote: string;
  /** 原文片段（带前后文）；没有原文时为空串。 */
  excerpt: string;
  /** `excerpt` 的三段切分，界面据此把命中部分包进 <mark>；没有原文时都是空串。 */
  before: string;
  hit: string;
  after: string;
  /** 高亮在原文里的码点区间；定位不到为 null。 */
  start: number | null;
  end: number | null;
  /** "第 3 页 · 第 120–168 字" 这类位置说明，单独一项都没有时为空串。 */
  where: string;
  /** 来源显示名，如"文档 · 线代第一章.pdf"。 */
  name: string;
  /**
   * 原文地址：非空时界面把它渲染成可点开的链接。
   *
   * 外部检索回来的来源（维基条目、arXiv 论文）才有这个值。没有它的引用仍然只能
   * 看摘录——"这句话出自哪篇论文"必须能自己核对，否则"自动填充或验证节点内容"
   * 只闭环了一半。
   */
  url: string | null;
}

/** UTF-16 下标 → 码点下标。 */
function codePointIndex(text: string, utf16Index: number): number {
  return Array.from(text.slice(0, utf16Index)).length;
}

/** 位置说明。页码与字符区间互不排斥，都有就都写。 */
export function refWhere(ref: SourceRef): string {
  const parts: string[] = [];
  if (ref.page != null) parts.push(`第 ${ref.page} 页`);
  if (ref.start != null && ref.end != null) parts.push(`第 ${ref.start + 1}–${ref.end} 字`);
  return parts.join(' · ');
}

/**
 * 一条引用在原文里的**全部**候选位置。
 *
 * `locateInText` 只回答"能不能定位、定到哪"，这条链路（点引用 → 跳到原文那一段）
 * 还需要回答两个它答不了的问题：
 *
 *   1. 这句引文在原文里出现 **几次**。后端在只有 `quote` 的引用上会先跑
 *      `_locate_quote` 并可能标 `SOURCE_QUOTE_AMBIGUOUS`（多处命中时不补坐标），
 *      所以前端拿到"有 quote、没有区间"时必须自己重数一遍——直接取 `indexOf`
 *      的第一处会跳到一个**没有依据**的位置，而且看起来和唯一定位一模一样。
 *   2. 定位依据是区间还是引文。界面要据此决定措辞：区间是后端校验过的坐标，
 *      引文是前端临时搜的；后者不该被说成"原文第 N 字"。
 *
 * `via` 的三种取值对应三种界面行为：
 *   - `range`：坐标可用，`others` 恒为空（有坐标就不再猜别处）；
 *   - `quote`：引文至少命中一处，`others` 是**其余**位置（含重叠），给"上一处/
 *     下一处"用；
 *   - `none`：定位不到。这时不能高亮，界面只能显示引文本身。
 */
export interface RefLocation {
  start: number;
  end: number;
  /** 同一句引文在原文里的其他起点（不含 `start` 自己）。 */
  others: number[];
  via: 'range' | 'quote' | 'none';
}

/**
 * 定位引用的全部候选。
 *
 * 区间优先，但**不盲信**：`start/end` 是后端生成时校验过的坐标，可它也会漂——
 * 手改过的导图、旧版本存下来的引用、或者材料被重新规范化过，都会让坐标指向
 * 另一段文字。这时按坐标高亮是一处**安静的错误**：界面把一个不相干的段落框成
 * "这句引文的出处"，而用户没有任何线索发现它错了。
 *
 * 所以有 `quote` 时先做一次逐字核对：
 *   - 对得上 → `via='range'`（坐标可信，就这样定位）；
 *   - 对不上 → 按引文重新搜（`via='quote'`，界面措辞改成"按引文搜到"）。
 *     搜不到才 `via='none'`——那时宁可不高亮，也不要框一段错的话。
 *
 * 没有 `quote` 的引用（旧数据里存在）只能信坐标：后端契约要求区间与引文至少
 * 有一样，`UNVERIFIED_SOURCE_REF` 会把两样都没有的剔掉。
 */
export function locateAll(text: string, ref: SourceRef): RefLocation {
  const none: RefLocation = { start: 0, end: 0, others: [], via: 'none' };
  if (!text) return none;
  const chars = Array.from(text);
  const rawQuote = ref.quote ?? '';
  const quote = rawQuote.trim();

  if (ref.start != null && ref.end != null && ref.end > ref.start && ref.end <= chars.length) {
    const excerpt = chars.slice(ref.start, ref.end).join('');
    // 三种都能认：没有引文（只能信坐标）、逐字相同、只差首尾空白。
    //
    // 第三种是**实测踩出来的**：模型引用的片段常常以空格或换行开头（英文论文的
    // 段落尤其如此），而这里比较时用的是 `trim()` 过的引用。只比 `excerpt === quote`
    // 会把一条坐标完全正确的引用判成"对不上"，然后退回全文搜索——搜到的是
    // 去掉前导空白之后的位置，于是高亮**少一个字**、位置说明偏 1、并且界面还会
    // 冒出一句"这条引用没有坐标"。首尾空白本来就不改变"指的是哪一段"，
    // 判定放行、坐标照用。
    if (!quote || excerpt === rawQuote || excerpt.trim() === quote) {
      return { start: ref.start, end: ref.end, others: [], via: 'range' };
    }
  }

  if (!quote) return none;

  const hits = quoteOffsets(text, quote);
  if (hits.length) {
    return { start: hits[0], end: hits[0] + Array.from(quote).length, others: hits.slice(1), via: 'quote' };
  }
  // 模型给的引文可能把换行压成了空格；按空白归一后再找一次。归一之后码点会变，
  // 只能拿它定位"大概是哪一段"，不能当精确坐标——所以 `via` 仍是 `quote`。
  const collapsed = text.replace(/\s+/g, ' ');
  const flat = quote.replace(/\s+/g, ' ');
  const flatAt = collapsed.indexOf(flat);
  if (flatAt >= 0) {
    const start = codePointIndex(collapsed, flatAt);
    return { start, end: start + Array.from(flat).length, others: [], via: 'quote' };
  }
  return none;
}

/**
 * `quote` 在 `text` 里的全部起点（码点下标，含重叠）。
 *
 * 重叠也算一处：`aa` 在 `aaa` 里有两次，把"多次命中"漏成一次会让界面笃定地
 * 指向其中一个位置。与后端 `_quote_occurrences` 同一口径。
 */
export function quoteOffsets(text: string, quote: string): number[] {
  if (!text || !quote) return [];
  const found: number[] = [];
  let at = text.indexOf(quote);
  while (at >= 0) {
    found.push(codePointIndex(text, at));
    at = text.indexOf(quote, at + 1);
  }
  return found;
}

/**
 * 在原文里定位引用。
 *
 * 优先用字符区间（后端给的就是它）；区间缺失、越界、或与引文逐字对不上时退回到
 * 按 `quote` 查找，这样手工编辑过的引用仍有救。两样都对不上就返回 null——调用方
 * 显示摘录，但别假装能高亮。
 *
 * 实现委托给 `locateAll`：两处必须同一套判据。分开写的话，摘要里高亮 A 段、
 * "看上下文"跳到 B 段，而界面上没有任何提示说这两件事不一致。
 */
export function locateInText(
  text: string,
  ref: SourceRef,
): { start: number; end: number } | null {
  const found = locateAll(text, ref);
  return found.via === 'none' ? null : { start: found.start, end: found.end };
}

/** 取原文片段：高亮区间前后各留 `pad` 个字，截断处加省略号。 */
export function excerptOf(text: string, start: number, end: number, pad = 60): string {
  const { before, hit, after } = windowOf(text, start, end, pad);
  return `${before}${hit}${after}`;
}

/**
 * 同上的三段形式。
 *
 * 界面要在原文片段里把命中的那句标出来，只有一整串字符串就没法插 <mark>；
 * 在这里切好，组件只负责摆放，不重复算下标。
 */
export function windowOf(
  text: string,
  start: number,
  end: number,
  pad = 60,
): { before: string; hit: string; after: string } {
  const chars = Array.from(text);
  const from = Math.max(0, start - pad);
  const to = Math.min(chars.length, end + pad);
  return {
    before: `${from > 0 ? '…' : ''}${chars.slice(from, start).join('')}`,
    hit: chars.slice(start, end).join(''),
    after: `${chars.slice(end, to).join('')}${to < chars.length ? '…' : ''}`,
  };
}

/**
 * 来源显示名：文件名优先，没有就用 kind 的中文名加序号。
 *
 * 三级退让，顺序有理由：
 *   1. `source_usage`——导图自己声明的来源清单，保存后重开也还在；
 *   2. 本次会话里那份 `Source`——外部检索刚抓回来的来源可能还没进 `source_usage`
 *      （比如生成失败但引用已经在树上），这时文件名比 `来源 s1a2b3c` 有用得多；
 *   3. 只有 id 时退回 `来源 <前 8 位>`：不给一个看起来像名字的假名字。
 */
export function refName(
  ref: SourceRef | string,
  usage: SourceUsage[],
  source: Source | null = null,
): string {
  const sourceId = typeof ref === 'string' ? ref : ref.source_id;
  const found = usage.find(item => item.source_id === sourceId);
  if (found) {
    const label = SOURCE_LABELS[found.kind] ?? '来源';
    return found.filename ? `${label} · ${found.filename}` : label;
  }
  if (source) {
    const label = SOURCE_LABELS[source.kind] ?? '来源';
    return source.filename ? `${label} · ${source.filename}` : label;
  }
  return `来源 ${sourceId.slice(0, 8)}`;
}

/**
 * 引用能不能点回原文。
 *
 * 两个来源依次退让：本次会话里那份来源的 `metadata.url`（最完整），然后是摘要
 * （`source_usage`）里的 `url`——保存过的旧图不带 `Source`，只把它留在 `metadata`
 * 上等于"这次能点、下次打开就没了"。
 *
 * 再判一次 http/https：后端契约已经过滤过一遍，但手写或导入的导图不经过那一道，
 * 而这个值会被直接塞进 `<a href>`。
 */
export function refUrl(
  ref: SourceRef,
  source: Source | null,
  usage: SourceUsage[],
): string | null {
  const fromSource = source?.metadata?.url ?? null;
  const candidate = fromSource
    ?? usage.find(item => item.source_id === ref.source_id)?.url
    ?? null;
  if (!candidate) return null;
  return /^https?:\/\//i.test(candidate) ? candidate : null;
}

/**
 * 用**指定的原文**把一条引用解析成可直接画的形状。
 *
 * `resolveRef` 的原文只能从本次会话的 `Source` 里找，而"点引用跳原文"这条链路
 * 会从知识库按 id 把正文取回来（见 `SourceContextDrawer`）——那份文本不在
 * `sources` 数组里，必须能从这里注入，否则库里的材料永远算不出高亮。
 *
 * `source` 只用来取 `filename` / `metadata.url`；正文以 `text` 为准，两者允许
 * 不一致（知识库里的正文才是 `start/end` 的坐标系）。
 */
export function resolveRefWithText(
  ref: SourceRef,
  text: string,
  source: Source | null,
  usage: SourceUsage[] = [],
): ResolvedRef {
  const located = text ? locateInText(text, ref) : null;
  const quote = ref.quote?.trim() || (located ? Array.from(text).slice(located.start, located.end).join('') : '');
  const window = located ? windowOf(text, located.start, located.end) : { before: '', hit: '', after: '' };

  return {
    ref,
    source,
    quote,
    excerpt: `${window.before}${window.hit}${window.after}`,
    before: window.before,
    hit: window.hit,
    after: window.after,
    start: located?.start ?? null,
    end: located?.end ?? null,
    where: refWhere(ref),
    name: refName(ref, usage, source),
    url: refUrl(ref, source, usage),
  };
}

/** 把一条引用解析成"能直接画"的形状。 */
export function resolveRef(ref: SourceRef, sources: Source[], usage: SourceUsage[] = []): ResolvedRef {
  const source = sources.find(item => item.id === ref.source_id) ?? null;
  return resolveRefWithText(ref, source?.text ?? '', source, usage);
}

/** 批量解析；`refs` 为空时返回空数组，调用方不必先判空。 */
export function resolveRefs(
  refs: SourceRef[] | undefined,
  sources: Source[],
  usage: SourceUsage[] = [],
): ResolvedRef[] {
  return (refs ?? []).map(ref => resolveRef(ref, sources, usage));
}

/** 一条"这个节点引用了这份来源"的记录。 */
export interface RefHit {
  /** 引用它的节点 id。 */
  id: string;
  /** 节点标题（未截断）。 */
  label: string;
  /** 从根到该节点的标题路径（含该节点），与详情面板的面包屑同口径。 */
  path: string[];
}

export interface RefIndex {
  /** source_id → 引用它的节点数。同一节点重复引用同一来源只算一次。 */
  counts: Record<string, number>;
  /** source_id → 引用它的节点，按先序排列（列表展开的顺序要稳定）。 */
  hits: Record<string, RefHit[]>;
}

/** 一张图的引用覆盖率：多少节点有依据、多少没有。 */
export interface RefCoverage {
  /** 可引用节点总数（与 `buildRefIndex` 同一套遍历，鱼骨/流程的各子树都算）。 */
  total: number;
  /** 至少有一条 `source_refs` 的节点数。 */
  cited: number;
  /** `cited / total`，`total` 为 0 时按 0 处理。 */
  ratio: number;
  /**
   * **没有引用的节点按来历分组**。这个分组是 `Node.origin` 存在的全部理由：
   * 同一个"12 个没有引用"的括号里，`generated` 那些是"可以补材料再生成一次"，
   * `manual` 那些是"本来就没有原文可引，不必处理"。合成一个数时界面只能把它们
   * 并列成一句免责声明——那是这个字段要消灭的那种话。
   *
   * 缺字段（旧图）按 `unknown` 算：那正是"不知道"的意思，不是 `manual`。
   */
  uncited: Record<NodeOrigin, number>;
}

/**
 * 全图的引用覆盖率。
 *
 * **为什么界面需要这个数**：一次生成里总有节点拿不到引文（实测薄批 78–80%、
 * 厚批 86%）。只给单个节点说"没有来源引用"，用户看到的是**一个洞**——他会怀疑
 * 是不是这个功能坏了、是不是这条引用点不开。把全图的口径摆出来，"这个节点没依据"
 * 才读得成"这张图有 20% 的节点是模型自己的话"，而那正是要让人知道的事。
 *
 * 口径与 `buildRefIndex` 完全一致（同一套 `rootsOf` 传进来的子树、同一套前序），
 * 两处数字才对得上：节点上画着角标、详情面板写着"没有引用"，覆盖率说 100%——
 * 三者必须来自同一次遍历。
 */
export function refCoverage(root: MindNode | readonly MindNode[] | null): RefCoverage {
  const uncited: Record<NodeOrigin, number> = { generated: 0, manual: 0, unknown: 0 };
  if (!root) return { total: 0, cited: 0, ratio: 0, uncited };
  const roots: readonly MindNode[] = Array.isArray(root) ? root : [root as MindNode];
  let total = 0;
  let cited = 0;
  const walk = (node: MindNode) => {
    total += 1;
    if ((node.source_refs ?? []).length) {
      cited += 1;
    } else {
      uncited[node.origin ?? 'unknown'] += 1;
    }
    for (const child of node.children) walk(child);
  };
  for (const item of roots) walk(item);
  return { total, cited, ratio: total ? cited / total : 0, uncited };
}

/**
 * 反向索引：从"来源"看"哪些节点引用了它"。
 *
 * 正向链路（节点 → 引用）已经在详情面板里了；这里补反向，是因为用户读材料时
 * 更关心"这段话被用到哪去了"。按先序遍历建，列表顺序与大纲顺序一致。
 *
 * 第一参数吃**一个根或一组根**：鱼骨与流程是多棵并列子树（`shape.rootsOf` 给的就是
 * 数组），而每条路径的起点是**它自己那棵子树的根**——一根骨里的引用不该在路径上
 * 出现另一根骨的内容（与后端 `path_to` 同一条口径）。树那边传一个根，行为与以前
 * 逐字节相同。
 */
export function buildRefIndex(root: MindNode | readonly MindNode[] | null): RefIndex {
  const index: RefIndex = { counts: {}, hits: {} };
  if (!root) return index;
  const roots: readonly MindNode[] = Array.isArray(root) ? root : [root as MindNode];

  const walk = (node: MindNode, path: string[]) => {
    const trail = [...path, node.label];
    const seen = new Set<string>();
    for (const ref of node.source_refs ?? []) {
      if (!ref.source_id || seen.has(ref.source_id)) continue;
      seen.add(ref.source_id);
      (index.hits[ref.source_id] ??= []).push({ id: node.id, label: node.label, path: trail });
      index.counts[ref.source_id] = (index.counts[ref.source_id] ?? 0) + 1;
    }
    for (const child of node.children) walk(child, trail);
  };

  for (const item of roots) walk(item, []);
  return index;
}
