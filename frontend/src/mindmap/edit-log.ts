/**
 * 编辑日志：把"用户改了什么"记成可以统计的事实。
 *
 * 这一版**只记录、不总结**，理由写在 `docs/` 之外的注释里也一样成立：
 * 后面那几步（LLM 总结 → DPO 微调）需要几百条偏好对，而一天几次的生成要攒几个月；
 * 但"改动集中在一两个具体问题上"（比如"80% 的改动是把长标题改短"）只要跑一次统计
 * 就能看出来，那种问题用规则一天就能修。所以先把事实攒下来。
 *
 * ## 为什么 diff 三份而不是只比节点
 *
 * 一次改动可能落在三处，任何一处漏了，统计就答不上"最常改什么"：
 *
 *  1. **节点字段**（label / detail / note / color / icon）—— 内容与样式；
 *  2. **树结构**（父变更、同层重排、新增、删除）；
 *  3. **边**（附加关系的增删改）。
 *
 * ## 三条判断，都影响统计怎么读
 *
 * **一个字段一条事件。** 改一个节点的 label 同时改 color 会产生 2 条，不是 1 条。
 * 合并成一条的话，"by_field" 就必须把一条事件同时算进两个桶，`sum(by_field)` 与
 * `by_kind.update` 对不上——统计一旦不能自校验，读的人就得先怀疑数字再怀疑结论。
 * 分开之后代价是"一次操作产生几条事件"这件事只留在 `at` 里（同一次提交共享同一个
 * 时刻字符串，`GROUP BY at` 能把它还原成一次操作）。
 *
 * **id 变了就是 remove + add，不做相似度匹配。** 节点 id 由 `newId()`（`n_` +
 * 随机）或后端分配，"删掉再加一个同名节点"必然是新 id。按 label 或位置去猜"这其实
 * 是同一次修改"会把统计变成一件需要解释算法的事，而它现在只需要读 SQL。
 *
 * **顺序变化只在"这一层成员没变"时才算 move。** 单纯比 sibling index 会炸：
 * 一次"在 50 个兄弟中间插一个"会让后面每个兄弟的 index 都 +1，于是 1 次真实操作
 * 变成 50 条 move，`by_kind` 立刻失真。所以 index 变化只有在**该父节点的子 id 集合
 * 完全没变**时才上报——那才是"重排"（agent 的 `move` op 带 index 就会走到这里）。
 *
 * ## 刻意不记的三件事
 *
 *  - **`title` 之外的整图字段**（`layout` / `theme` / `source_usage` / `relations`）。
 *    换朝向（`setDirection`）一次会产生一条噪音，而它回答不了"生成侧该改什么"。
 *  - **撤销 / 重做**：一次被撤销的编辑不是用户的最终意图。记了它，`label_shrunk`
 *    会同时收到"18→9"和"9→18"两条，平均值被拉回中性、次数却翻倍——比不记更糟。
 *    要审计"用户反复撤销"该是另一个指标，这一版不做。
 *  - **create 意图落画布的整棵树**（空画布 → 一整棵树）：整棵树都是"新增"，
 *    那不是 diff，是把整棵树重念一遍。与 `stores/mindmap.ts::diffCandidate` 在没有
 *    基线时返回 `null` 是同一条判断。**注意这条只否掉了"整棵树"这个载荷，
 *    没有否掉"生成"这个动作本身**——动作记成一条 `generate`（见下）。
 *
 * ## 为什么必须有一条 `generate`：它是"一字未改"这个最强正样本的唯一落点
 *
 * 生成结果是**直接进画布**的（没有候选、没有「应用」这一步）。`create` 那次落地
 * 的 diff 是 0 条，于是"生成完一字不改直接保存"在表里是 0 行，与"这个用户从没用过"
 * 完全不可区分。**缺的不是样本，是分母**：10 张图里 9 张一字未改、1 张改 10 处，
 * 只统计改动会得出"标题被普遍改短"的结论，然后去压生成侧的上限——而那 9 张本来
 * 合适的图在表里根本没出现过，偏差方向永远朝"生成质量更差"。
 *
 * 所以每次生成落地都记**一条** `generate`，不管改了几处：
 *
 *  - `nodeCount` 是这次落进画布的那棵树有几个节点，**在生成完成那一刻定死**。
 *    保存时再数就数错了——那时用户已经改过，数出来的是"改后的规模"，分母永久的偏。
 *  - `runId` 让它与"这次生成带来的那批改动"对上：查"有没有对应 `run_id` 的改动
 *    事件"就知道这次生成被改没被改，不需要前端把"改了几处"算好塞进来
 *    （那是 `changeCount` 干过的事，现在不用了）。
 *
 * 后端统计口径（`app/mindmaps/edits.py::summary`）：
 * 分母 `SUM(node_count) WHERE kind='generate'`，分子是该图上的节点改动条数，
 * 零改动次数是"那个 `run_id` 下一条改动事件都没有"的 generate 行数。
 */
import type { DiagramContent, MindEdge, MindNode } from '../api/types';
import { charLength, edgesOf } from './edits';
import { rootsOf } from './shape';

/** 谁改的。`user` 是手工编辑（改文字、加节点、删节点、连线），`agent` 是生成落地。 */
export type EditActor = 'user' | 'agent';

/**
 * 改动种类。
 *
 * `update` 是节点**内容**，`style` 是节点**外观**。分成两种而不是都叫 `update`：
 * "用户在调样式上花了多少改动"本身是个信号（样式占比高说明生成侧的视觉默认值不对），
 * 混进 update 就再也拆不出来了。
 *
 * `generate` 不是"改动"，是**一次生成落到画布**：它没有节点、没有字段、没有层数，
 * 只有 `nodeCount` 与 `runId`。它和其余六种并列而不是另开一张表，因为它回答的是
 * 同一个问题的另一半——"改了什么"要有"本来可以不改"作分母。
 *
 * `accept` 是它的前身（候选层还在时"用户点了应用"）。**前端不再产生它**，保留在
 * 联合类型里只是为了与后端 `EditKind` 逐字对齐：库里还有几千行历史，镜像少写一个
 * 字面量，读老数据的人就会以为那些行是非法的。
 *
 * `repair` 是**一键修复**（`replaceContent`，规则批量整理整棵树）。它不是"哪一处
 * 被改了"，而是"这一次动作是规则干的"：`replaceContent` 落地时用 `DiffOptions.kind`
 * 把这一整批事件的 `kind` 全部换成 `'repair'`，`actor` 仍是 `user`（扳机是用户按的）。
 * 于是 `by_kind` 少掉"这次修复删了几个、搬了几个"的分桶，换来的是一个干净的事实：
 * **用户回看"我改过什么"时，不会看到自己没做过的改动。**
 * 动作粒度没丢——`field` / `before` / `after` / `depth` / `subtreeSize` 都还在事件行上，
 * 只是不能再按 `kind` 分组读。理由与代价见 `docs/OPTIMIZATION-REPAIR.md` §7。
 */
export type EditKind =
  | 'add' | 'remove' | 'update' | 'move' | 'edge' | 'style' | 'accept' | 'generate'
  | 'repair';

/**
 * 改动落在哪个字段上。**只有 `kind` 为 `update` / `style` 的事件才有值。**
 *
 * 这条规则让统计能自校验：`sum(by_field) === by_kind.update + by_kind.style`。
 * 新增与删除改的其实是父节点的 `children`，但把它们也标成 `children` 会让上面那条
 * 恒等式失效，而 `by_kind` 已经把"加了多少、删了多少"说清楚了。
 *
 * `title` 是导图标题（不是节点标题）：用户改标题也是一次真实改动，漏掉它，统计就
 * 回答不了"最常改什么"。`label_shrunk` 只统计 `field='label'`，两者不会混。
 */
export type EditField = 'label' | 'title' | 'detail' | 'note' | 'color' | 'icon';

/**
 * 一条改动。
 *
 * `before` / `after` 是**摘要**，不是内容：
 *  - `label` / `title` 存全文（≤200 字，统计要算长度，截了就没法算）；
 *  - `detail` / `note` 只存长度（`"1800字"`）——2000 字的正文进日志会把这张表撑成
 *    第二个 `mindmaps.content`，而"从 1800 字改成 700 字"这件事本身就是情报；
 *  - `color` / `icon` 存原值或 `"无"`；
 *  - `move` 存父节点标题（`"风险"` / `"根"`）或位次（`"第2位"`）；
 *  - `edge` 存 `"A → B（是）"`。
 */
export interface EditEvent {
  actor: EditActor;
  kind: EditKind;
  /** 节点 id。`edge` 事件填边的**源**节点，`title` 事件没有节点。 */
  nodeId?: string;
  field?: EditField;
  before?: string;
  after?: string;
  /**
   * 改动之后，这个节点在树上的**父节点 id**（`null` = 它是某棵子树的根）。
   *
   * 只有 `kind === 'add'` 现在填它，而这正是它存在的全部理由：**"用户新加的节点
   * 挂在哪"原本在表里是一条都没有的可判事实**。改动前的统计里 565 条事件是
   * update 559 / style 4 / remove 2，`add` 一条都没有——不是用户从没加过节点，
   * 而是加节点这件事从来没被记成一行。（`move` 的父变化已经写在 `before` /
   * `after` 的父节点标题里，那是给人读的摘要；要不要也给它一个 id 事实，等有
   * 真的 `add` 样本之后再谈，现在加了也没有读者。）
   *
   * **它是 `null` 与"这个字段没值"是两件事**：`undefined` 表示这类事件不记父节点
   * （update / style / remove / edge / title / generate），`null` 表示它就是根。
   * 混成一个值，读的人就分不清"根"和"没记"。
   *
   * 口径与 `indexForest` 一致：鱼骨的六根骨、流程的各阶段都是**自己的根**
   * （`parentId: null`），哪怕它在画布上挂在主骨上——那层挂载关系不在 `Node` 的
   * 父子关系里（与后端 `agent/shapes.py::find_parent` 同一条判断）。
   */
  parentId?: string | null;
  /**
   * 涉及的层数，**根是 1**（与契约"最多 8 层"同一口径）。
   *
   * `remove` 填删除前所在层，`add` / `move` / `update` / `style` 填改动后的层。
   * 统计里的 `removed_depth` 直接读它，回答"删掉的集中在第几层"——第 3 层最多
   * 就说明初次生成挖得太深。
   *
   * **它只对"零散删除"有意义**：一条 `remove` 现在代表一整棵被删的子树，根在第 3 层
   * 不等于"第 3 层的内容被嫌弃"。所以 `removed_depth` 只统计 `subtreeSize === 1`，
   * 成规模的删除进 `removed_subtree`（见 `subtreeSize`）。
   */
  depth?: number;
  /**
   * 这条 `remove` 一共删掉几个节点（**含它自己**）。只有 `kind === 'remove'` 有值。
   *
   * 删一个叶节点是 `1`；删一整支 12 个节点是**一条**事件、`subtreeSize === 12`。
   * "每个后代各记一条"会让 `removed_depth` 在 2…5 层各留几条，读起来像"每层都在被
   * 删"，而真实动作只有一个——于是"初次生成挖太深"这个结论直接不成立（不是方向偏）。
   *
   * 数的是**真的消失了**的节点：删除时子节点被改派到别处活下来，只有根自己算 1。
   */
  subtreeSize?: number;
  /**
   * 这次生成落进画布的那棵树有几个节点。只有 `kind === 'generate'` 有值。
   *
   * **在生成完成那一刻算好并定死**：保存时才数会数到改后的树——一次"删掉一半再
   * 保存"会把分母算成改后的规模，比例永久偏高，而分母存在的意义正是"模型给了
   * 多少东西"。
   *
   * 它不进 `after`：`after` 的语义是"截断后的文本摘要"，把计数塞进去会让
   * `CAST(after AS INTEGER)` 对每一条文字摘要静默返回 0——一个看起来合理、
   * 实际全错的聚合，正是这张表最不能出的错。
   */
  nodeCount?: number;

  /** 这次改动动的是哪个 run 的产物。生成落地与它带来的那批改动共用同一个 id。 */
  runId?: string;
  /** ISO 时刻。**同一次提交内所有事件共享同一个值**，它是"一次操作"的分组键。 */
  at: string;
}

/**
 * 单次保存最多带多少条事件。
 *
 * 500 是"一次生成用户可能改 20 处"的 25 倍，正常永远碰不到；它防的是"一次删掉
 * 整棵树"把请求体撑到几 MB。超出的部分**留在队列里等下一次保存**（是分批，不是
 * 丢弃），只把条数（`truncated`）上报给服务端记一条 warning——不在表里插一条假
 * 事件，因为统计的 `total` 必须是"真实改动数"。
 */
export const EDIT_EVENT_LIMIT = 500;

/** 摘要里标题的截断长度。日志是给人看的一行，不需要整段标题。 */
const SUMMARY_LABEL_CHARS = 40;

interface Indexed {
  node: MindNode;
  parentId: string | null;
  /** 根是 1。 */
  depth: number;
  /** 在父节点 `children` 里的下标。 */
  index: number;
}

interface TreeIndex {
  entries: Map<string, Indexed>;
  /**
   * 每个节点的"子 id 集合指纹"（排序后拼起来）。
   *
   * 它只服务一件事：区分"重排"与"插入导致的 index 位移"。前者成员集合不变，
   * 后者变了。
   */
  siblingKeys: Map<string, string>;
}

function fingerprint(children: readonly MindNode[]): string {
  return children.map(child => child.id).sort().join('\u0000');
}

/**
 * 多棵**并列**子树的索引（鱼骨的六根骨、流程的各阶段）。
 *
 * 一份 `entries` 穿到底，所以"节点 id 全局唯一"这件事在索引里自动成立（后写的覆盖
 * 先写的，与契约层"六棵骨里 id 必须唯一"同一条前提）。每棵子树各自从第 1 层数，
 * 与 `_scan_tree` 的口径一致——一根骨挂在主骨上，但画布上它是从自己的根展开的一列。
 *
 * 树只有一棵，`indexForest([root])` 与原来的 `indexTree(root)` 完全等价（每一格都
 * 一样，`index` 都是 0），所以树的日志一条都不该变。
 */
function indexForest(roots: readonly MindNode[]): TreeIndex {
  const entries = new Map<string, Indexed>();
  const siblingKeys = new Map<string, string>();

  const walk = (node: MindNode, depth: number) => {
    siblingKeys.set(node.id, fingerprint(node.children));
    node.children.forEach((child, index) => {
      entries.set(child.id, { node: child, parentId: node.id, depth: depth + 1, index });
      walk(child, depth + 1);
    });
  };

  roots.forEach((root, index) => {
    entries.set(root.id, { node: root, parentId: null, depth: 1, index });
    walk(root, 1);
  });
  return { entries, siblingKeys };
}

/**
 * 这棵（旧树上的）子树里**真的消失了**几个节点，含根自己。
 *
 * 不能直接用"子树节点数"：一个节点被删掉、它的子节点却被改派到别处活下来时，
 * 只有它自己消失，`subtreeSize` 就该是 1。所以逐个 id 对着新树查——树的结构保证
 * "后代还在 ⇒ 祖先还在"，于是递归能如实数出跟着一起走的那些人。
 *
 * 只有**被删子树的根**会调用它（后代不是根，不重复走），所以整体仍是 O(n)。
 */
function vanishedSize(node: MindNode, newIndex: TreeIndex): number {
  let count = newIndex.entries.has(node.id) ? 0 : 1;
  for (const child of node.children) count += vanishedSize(child, newIndex);
  return count;
}

function clip(text: string): string {
  const chars = Array.from(text);
  return chars.length > SUMMARY_LABEL_CHARS
    ? `${chars.slice(0, SUMMARY_LABEL_CHARS).join('')}…`
    : chars.join('');
}

/** 父节点在摘要里怎么称呼。找不到（不该发生）退回 id，好过让一行日志空着。 */
function nameAt(index: TreeIndex, id: string | null): string {
  if (!id) return '根';
  const entry = index.entries.get(id);
  return entry ? clip(entry.node.label.trim()) : id;
}

/** `detail` / `note` 只进长度。`"0字"` 表示被清空，读的时候不用再猜。 */
function charsOf(text: string | null | undefined): string {
  return `${charLength((text ?? '').trim())}字`;
}

function visualOf(value: string | null | undefined): string {
  return value ?? '无';
}

function describeEdge(edge: MindEdge, index: TreeIndex): string {
  const text = `${nameAt(index, edge.source)} → ${nameAt(index, edge.target)}`;
  const label = (edge.label ?? '').trim();
  return label ? `${text}（${label}）` : text;
}

export interface DiffOptions {
  actor: EditActor;
  /** 同一次提交共享的时刻（`new Date().toISOString()` 由调用方取一次）。 */
  at: string;
  runId?: string;
  /**
   * 这一次动作的 `kind`：给了它就**覆盖**每条事件自己的 `kind`。
   *
   * 只有 `repair` 用（一键修复把整棵树换掉，diff 出来的是 add / remove / move /
   * update 那批动作，但整批只回答一件事："这一次是规则批量整理"）。缺省不给，
   * 每条事件保留 `diffEdits` 自己判出来的动作名——手工编辑那条路一个字都不变。
   */
  kind?: EditKind;
}

/**
 * 两份内容之间的改动清单。
 *
 * `before` 或 `after` 为空（create 意图、空画布）时返回空数组：整棵树都是新的，
 * 不是 diff。见文件头那段"刻意不记"。
 *
 * 参数是 `DiagramContent`：鱼骨与流程是**多棵并列子树**，走 `indexForest(rootsOf())`。
 * 树的节点、结构、边三条判据在两种非树形状下依然是同一套算法——"改了什么"与"这张图
 * 按什么读"无关，日志里只需要节点 id、层号与父 id 这些两种形状都有的东西。
 */
export function diffEdits(
  before: DiagramContent | null,
  after: DiagramContent | null,
  options: DiffOptions,
): EditEvent[] {
  if (!before || !after) return [];

  const events: EditEvent[] = [];
  const emit = (event: Omit<EditEvent, 'actor' | 'at' | 'runId'>) => {
    events.push({
      actor: options.actor,
      at: options.at,
      ...(options.runId ? { runId: options.runId } : {}),
      ...event,
      // 放在 `...event` **之后**：`options.kind` 给了就压过动作名（见 `DiffOptions`）。
      // 这是这个文件里唯一一处"动作名可以被外部改写"的地方，所以只有一处。
      ...(options.kind ? { kind: options.kind } : {}),
    });
  };

  const oldTitle = (before.title ?? '').trim();
  const newTitle = (after.title ?? '').trim();
  if (oldTitle !== newTitle) {
    emit({ kind: 'update', field: 'title', before: oldTitle, after: newTitle });
  }

  const oldIndex = indexForest(rootsOf(before));
  const newIndex = indexForest(rootsOf(after));

  for (const [id, entry] of newIndex.entries) {
    const old = oldIndex.entries.get(id);
    if (!old) {
      // `parentId` 是这条事件唯一能回答"新节点挂在谁下面"的字段。`after` 只是
      // 标签摘要（截到 40 字），拿它去猜父节点等于让统计变成一件需要解释算法的
      // 事，而它现在只需要读一行。
      emit({
        kind: 'add', nodeId: id, after: clip(entry.node.label.trim()),
        depth: entry.depth, parentId: entry.parentId,
      });
      continue;
    }

    const oldLabel = old.node.label.trim();
    const newLabel = entry.node.label.trim();
    if (oldLabel !== newLabel) {
      emit({
        kind: 'update', field: 'label', nodeId: id,
        before: oldLabel, after: newLabel, depth: entry.depth,
      });
    }
    if ((old.node.detail ?? '').trim() !== (entry.node.detail ?? '').trim()) {
      emit({
        kind: 'update', field: 'detail', nodeId: id,
        before: charsOf(old.node.detail), after: charsOf(entry.node.detail), depth: entry.depth,
      });
    }
    if ((old.node.note ?? '').trim() !== (entry.node.note ?? '').trim()) {
      emit({
        kind: 'update', field: 'note', nodeId: id,
        before: charsOf(old.node.note), after: charsOf(entry.node.note), depth: entry.depth,
      });
    }
    if ((old.node.color ?? null) !== (entry.node.color ?? null)) {
      emit({
        kind: 'style', field: 'color', nodeId: id,
        before: visualOf(old.node.color), after: visualOf(entry.node.color), depth: entry.depth,
      });
    }
    if ((old.node.icon ?? null) !== (entry.node.icon ?? null)) {
      emit({
        kind: 'style', field: 'icon', nodeId: id,
        before: visualOf(old.node.icon), after: visualOf(entry.node.icon), depth: entry.depth,
      });
    }

    if (old.parentId !== entry.parentId) {
      emit({
        kind: 'move', nodeId: id,
        before: nameAt(oldIndex, old.parentId), after: nameAt(newIndex, entry.parentId),
        depth: entry.depth,
      });
      // 根没有"位次"（index 恒 0），所以下面那条 `old.parentId !== null` 不是补丁，
      // 是这条判据的适用前提。
    } else if (
      old.parentId !== null
      && old.index !== entry.index
      // 只有"这一层成员没变"才是重排；插一个兄弟会让后面所有人的 index 都变。
      && oldIndex.siblingKeys.get(old.parentId) === newIndex.siblingKeys.get(old.parentId)
    ) {
      emit({
        kind: 'move', nodeId: id,
        before: `第${old.index + 1}位`, after: `第${entry.index + 1}位`, depth: entry.depth,
      });
    }
  }

  for (const [id, entry] of oldIndex.entries) {
    if (newIndex.entries.has(id)) continue;
    // 只给"被删子树的根"记一条。父节点也没了的那些后代是这棵树的一部分，它们的
    // 删除已经被祖先那条事件涵盖了；每个后代各记一条，`removed_depth` 就会在
    // 2…5 层各留几条，读起来像"每层都在被删"，而真实动作只有一个：删掉了一整支。
    if (entry.parentId !== null && !newIndex.entries.has(entry.parentId)) continue;
    emit({
      kind: 'remove', nodeId: id,
      before: clip(entry.node.label.trim()), depth: entry.depth,
      subtreeSize: vanishedSize(entry.node, newIndex),
    });
  }

  const oldEdges = new Map(edgesOf(before).map(edge => [edge.id, edge]));
  const newEdges = new Map(edgesOf(after).map(edge => [edge.id, edge]));

  for (const [id, edge] of newEdges) {
    const previous = oldEdges.get(id);
    if (!previous) {
      emit({ kind: 'edge', nodeId: edge.source, after: describeEdge(edge, newIndex) });
      continue;
    }
    // 端点不变而 label / kind 变了：还是同一条线，只是换了画法与说明。
    if ((previous.label ?? null) !== (edge.label ?? null)
      || (previous.kind ?? null) !== (edge.kind ?? null)
      || previous.source !== edge.source || previous.target !== edge.target) {
      emit({
        kind: 'edge', nodeId: edge.source,
        before: describeEdge(previous, oldIndex), after: describeEdge(edge, newIndex),
      });
    }
  }
  for (const [id, edge] of oldEdges) {
    if (newEdges.has(id)) continue;
    emit({ kind: 'edge', nodeId: edge.source, before: describeEdge(edge, oldIndex) });
  }

  return events;
}

/** `generateEvent` 需要的东西：时刻与 run。actor 固定 `agent`，见下。 */
export interface GenerateOptions {
  /** 与这次生成带来的那批改动**同一个** `at`（`GROUP BY at` 才能还原成一次操作）。 */
  at: string;
  runId: string;
}

/**
 * "这次生成落到画布"这条事件：每次生成记一条，`nodeCount` 是落进画布的节点数。
 *
 * **actor 固定 `agent`**，而不是用这次差分的 actor：产出这棵树的是模型。同一批里
 * 那些节点改动也是 `agent`（用户没动手），两类事件靠 `kind` 分开，不靠 actor——
 * 混成同一个 kind，`by_kind` 就再也分不清"改了 3 处"和"生成了 1 次"。
 *
 * `runId` 这里**必填**（`AcceptOptions` 里它是可选的）：没有 run_id 的 generate 行
 * 不算"一字未改"，只能算"当时没记"，后端统计会把它从那个指标里排掉。
 */
export function generateEvent(nodeCount: number, options: GenerateOptions): EditEvent {
  return {
    actor: 'agent',
    kind: 'generate',
    at: options.at,
    nodeCount,
    runId: options.runId,
  };
}
