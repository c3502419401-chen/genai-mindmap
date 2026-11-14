import type {
  MindMapContent,
  MindMapOp,
  MindNode,
  OpBatch,
  SourceRef,
  SourceUsage,
} from '../api/types';
import { charLength, cloneTree, findNode, findParent, LIMITS, removeNode } from './edits';

/**
 * 导图操作执行器（前端侧）。
 *
 * 契约（AddOp / UpdateOp / RemoveOp / MoveOp / OpBatch）在 `api/types.ts`，
 * 那里是后端契约的镜像。本文件只放执行实现，避免契约与实现互相依赖。
 *
 * 需要契约类型的地方继续从本文件 import 也可以——下面做了 re-export，
 * 现有调用方的路径不用改。
 */
export type { AddOp, MindMapOp, MoveOp, OpBatch, RemoveOp, UpdateOp } from '../api/types';

/** 单个 op 失败。`path` 形如 `ops.2.id`，会作为工具结果回给模型。 */
export class OpError extends Error {
  constructor(
    readonly code: string,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = 'OpError';
  }
}

export interface ApplyResult {
  content: MindMapContent;
  /** 被改动的节点 id，用于引用重校验与局部刷新。 */
  changed_ids: string[];
}

// ---------------------------------------------------------------------------
// 执行器
// ---------------------------------------------------------------------------

/**
 * 深查找直接复用 `edits.ts` 的实现：同一条 id 语义只该有一份代码，
 * 否则编辑器与 op 执行器迟早会在"根节点算不算命中"这种细节上分叉。
 */
export { findNode as findDeep, findParent as findParentDeep };

/**
 * `candidateId` 是否是 `ancestorId` 的子孙，**含自己**。
 *
 * 含自己不是省事：`move` 里"挂到自己"和"挂到自己的子孙"在语义上是同一个错误，
 * 拆成两条判断迟早漏掉一条。
 */
export function isDescendant(root: MindNode, ancestorId: string, candidateId: string): boolean {
  const ancestor = findNode(root, ancestorId);
  if (!ancestor) return false;
  return findNode(ancestor, candidateId) !== null;
}

/**
 * 完整的随机 id（UUID 的 32–36 位随机量）。
 *
 * 不复用 `edits.ts::newId()`：那个为了界面可读把 UUID 截成 8 位，
 * 界面上手点一次不会撞，但一批 op 里几十个新节点时碰撞概率不再可以忽略；
 * 一旦撞车整批会被 `duplicate_id` 打回，模型只会看到"随机失败"。
 */
function fullRandomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // 非安全上下文（http://）拿不到 randomUUID：退回同量级的随机十六进制。
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * 在指定父节点的子列表里插入一个节点，返回新的根。
 *
 * `edits.ts` 的 `replaceChildren` 没有导出，而"按 index 插入"只有 op 执行器需要，
 * 所以在这里单独写一个；做法仍是结构共享，调用方拿到的旧树不受影响。
 */
function insertChildAt(root: MindNode, parentId: string, index: number, child: MindNode): MindNode {
  if (root.id === parentId) {
    const children = [...root.children];
    children.splice(index, 0, child);
    return { ...root, children };
  }
  return {
    ...root,
    children: root.children.map(item => insertChildAt(item, parentId, index, child)),
  };
}

/**
 * 整批 op 执行完后的整树校验。
 *
 * 为什么放在最后而不是每条 op 里：add/move 单条合法、合起来越界的组合很常见
 * （比如一条 batch 里补 30 个节点），逐条校验会在中途误报；但整批交付的树
 * 一旦越界，后端 Pydantic 会 422，所以这里必须兜住。
 *
 * 失败一律抛 `tree_invalid` + path `ops`：没有任何一条 op 能单独定位到问题，
 * 硬塞一个下标只会误导模型去改一条本来正确的 op。
 */
function assertTreeValid(root: MindNode, sourceUsage: SourceUsage[]): void {
  const invalid = (message: string): never => {
    throw new OpError('tree_invalid', 'ops', message);
  };

  // 后端 MindMapContent.validate_tree 还会校验"节点引用的来源必须在 source_usage 里"。
  // 前端不做这一步的话，脏引用会一路放到 POST 才 422，而那时模型已经拿着"成功"
  // 的反馈继续改了。
  const allowedSources = new Set(sourceUsage.map(item => item.source_id));
  const seen = new Set<string>();
  const stack: Array<{ node: MindNode; depth: number }> = [{ node: root, depth: 1 }];

  while (stack.length > 0) {
    const { node, depth } = stack.pop()!;

    if (seen.has(node.id)) invalid(`节点 ID 必须唯一，重复的是 ${node.id}`);
    seen.add(node.id);
    if (seen.size > LIMITS.nodes) invalid(`导图最多 ${LIMITS.nodes} 个节点`);
    if (depth > LIMITS.depth) invalid(`导图最多 ${LIMITS.depth} 层`);

    // 非空按 trim 后的值判；长度按**原始** label 算——后端 `Field(max_length=200)`
    // 量的也是原始值，前端用 trim 后的长度会把「200 字 + 一个尾随空格」放过去，
    // 下一次保存就被 422 打回。
    const label = node.label.trim();
    if (!label) invalid(`节点 ${node.id} 的标题不能为空`);
    if (charLength(node.label) > LIMITS.label) {
      invalid(`节点 ${node.id} 的标题超过 ${LIMITS.label} 个字`);
    }
    if (node.children.length > LIMITS.children) {
      invalid(`节点 ${node.id} 最多 ${LIMITS.children} 个子节点`);
    }
    for (const ref of node.source_refs ?? []) {
      if (!allowedSources.has(ref.source_id)) {
        invalid(`节点 ${node.id} 引用的来源 ${ref.source_id} 不在 source_usage 中`);
      }
      // 与 SourceRef.validate_range 同口径：start / end 要么都给、要么都不给，
      // 且区间不能为空（end 必须严格大于 start）。
      const start = ref.start ?? null;
      const end = ref.end ?? null;
      if ((start === null) !== (end === null)) {
        invalid(`节点 ${node.id} 的引用 start 和 end 必须同时提供`);
      }
      if (start !== null && end !== null && end <= start) {
        invalid(`节点 ${node.id} 的引用 end 必须大于 start`);
      }
    }

    for (const child of node.children) stack.push({ node: child, depth: depth + 1 });
  }
}

/**
 * 按序执行一整批 op，返回新内容与被触及的 id。
 *
 * 整批原子生效：全部操作都在 `cloneTree` 出来的副本上做，任何一条抛 `OpError`
 * 就把副本整个丢掉。调用方不可能拿到"改了一半"的树 —— 半批生效会让树的 shape
 * 和模型以为的不一致，而模型下一轮会基于那个错误前提继续改，越修越远。
 *
 * 顺序敏感是刻意的：后面的 op 可以引用前面 `add` 出来的 id，这正是一批 op 的
 * 意义所在（先建父节点再往里挂子节点）。也正因为如此，中间没有"延迟解析"，
 * 每条 op 都拿当时的树做判断。
 */
export function applyOps(content: MindMapContent, batch: OpBatch): ApplyResult {
  const next: MindMapContent = cloneTree(content);
  const changed = new Set<string>();

  batch.ops.forEach((op, index) => {
    /** 失败路径回给模型，形如 `ops.2.parent_id`；下标必须是本批里的真实位置。 */
    const path = (field: string) => `ops.${index}.${field}`;

    switch (op.op) {
      case 'add': {
        const parent = findNode(next.root, op.parent_id);
        if (!parent) {
          throw new OpError('parent_not_found', path('parent_id'), `父节点 ${op.parent_id} 不存在`);
        }
        if (findNode(next.root, op.id)) {
          throw new OpError('duplicate_id', path('id'), `节点 ID ${op.id} 已经存在`);
        }
        // 上界取 length：等于长度就是追加到末尾，与 splice 的语义一致。
        if (!Number.isInteger(op.index) || op.index < 0 || op.index > parent.children.length) {
          throw new OpError(
            'index_out_of_range',
            path('index'),
            `插入位置 ${op.index} 超出范围 0..${parent.children.length}`,
          );
        }

        const node: MindNode = {
          id: op.id,
          label: op.label,
          children: [],
          // 缺省给空数组而不是 undefined：模型漏写这一项时，节点不该凭空继承
          // 任何引用，也不该让下游 `node.source_refs.length` 抛错。
          source_refs: cloneTree(op.source_refs ?? []),
          // `detail` 是生成产出的说明，与用户备注 `note` 分开存；`add` 不带
          // note 字段，正是为了这两条写入路径永不交叉。
          detail: op.detail ?? null,
        };
        next.root = insertChildAt(next.root, op.parent_id, op.index, node);
        changed.add(op.id);
        // 父节点的 children 变了，局部刷新要能重排它的分支；
        // 只报新节点 id 的话，新增节点的父分支不会被重算。
        changed.add(op.parent_id);
        break;
      }

      case 'update': {
        const node = findNode(next.root, op.id);
        if (!node) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        const changes: { label?: string; source_refs?: SourceRef[]; detail?: string | null } =
          op.changes ?? {};
        // 后端 UpdateChanges.at_least_one 直接拒绝空 changes。前端不拦的话，
        // 同一条 op 在这里成功、在服务端 ValidationError，模型收到的反馈相反。
        if (
          changes.label === undefined &&
          changes.source_refs === undefined &&
          changes.detail === undefined
        ) {
          throw new OpError('empty_changes', path('changes'), `节点 ${op.id} 的 update 至少要改一个字段`);
        }
        // `undefined` 是"这一项不动"，`[]` 是"显式清空"。这两句必须分开写，
        // 合并成 `node.source_refs = changes.source_refs ?? node.source_refs`
        // 会把模型的清空意图当成漏字段吞掉。
        if (changes.label !== undefined) node.label = changes.label;
        if (changes.source_refs !== undefined) {
          node.source_refs = cloneTree(changes.source_refs);
        }
        // `detail` 同一套约定：`undefined` 不动，空串归一成 `null`（清空）。
        // 这里**不碰 note**：那是用户的备注，op 通道无权重写它。
        if (changes.detail !== undefined) {
          const detail = (changes.detail ?? '').trim();
          node.detail = detail ? detail : null;
        }
        changed.add(op.id);
        break;
      }

      case 'remove': {
        if (op.id === next.root.id) {
          throw new OpError('cannot_remove_root', path('id'), '根节点不能删除');
        }
        if (!findNode(next.root, op.id)) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        next.root = removeNode(next.root, op.id);
        changed.add(op.id);
        break;
      }

      case 'move': {
        const target = findNode(next.root, op.id);
        if (!target) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        const newParent = findNode(next.root, op.new_parent_id);
        if (!newParent) {
          throw new OpError(
            'parent_not_found',
            path('new_parent_id'),
            `新的父节点 ${op.new_parent_id} 不存在`,
          );
        }
        // isDescendant 含自己，所以"挂到自己"和"挂到自己的子孙"都由这一句拦住。
        // 顺带拦住根节点：根的任何一个候选父节点要么是它自己、要么是它的子孙。
        if (isDescendant(next.root, op.id, op.new_parent_id)) {
          throw new OpError(
            'cycle_detected',
            path('new_parent_id'),
            `节点 ${op.id} 不能挂到 ${op.new_parent_id} 下：会形成环`,
          );
        }

        // 同父内移动按"先摘除、再插入"执行，所以合法上界是摘除后的长度，
        // 也就是 length - 1；跨父移动不缩短新父节点的列表，上界仍是 length。
        // 这样 `index` 对调用方只有一种解释，不用记"同父时要减一"。
        const oldParent = findParent(next.root, op.id);
        const sameParent = oldParent !== null && oldParent.id === op.new_parent_id;
        const limit = sameParent ? newParent.children.length - 1 : newParent.children.length;
        if (!Number.isInteger(op.index) || op.index < 0 || op.index > limit) {
          throw new OpError(
            'index_out_of_range',
            path('index'),
            `插入位置 ${op.index} 超出范围 0..${limit}`,
          );
        }

        next.root = insertChildAt(removeNode(next.root, op.id), op.new_parent_id, op.index, target);
        changed.add(op.id);
        changed.add(op.new_parent_id);
        // 旧父节点的子列表也变了，局部刷新要能重排它的分支。
        if (oldParent) changed.add(oldParent.id);
        break;
      }

      default: {
        // 契约外的 op 类型（后端将来加新 op，或模型自己编了一个）：静默跳过等于
        // 假装成功，模型会拿着"已生效"的反馈继续往下改。必须明确报错。
        const unknown = op as { op: string };
        throw new OpError('unknown_op', path('op'), `未知的 op 类型 ${unknown.op}`);
      }
    }
  });

  assertTreeValid(next.root, next.source_usage ?? []);

  return { content: next, changed_ids: Array.from(changed) };
}

/**
 * 把模型草案里的临时 id（`$tmp1` 这类）换成真实 id，并改写所有引用。
 *
 * 两件必须做到的事：一是同一个临时 id 无论被引用多少次都映射到同一个真实 id
 * （否则 add 出来的父节点和后面的子节点指向两个不同节点，树会静默分叉）；
 * 二是 `parent_id` 可能出现在它的定义之前，所以映射要在第一次遇到时就确定，
 * 而不是先扫一遍 add 再改引用。
 *
 * 非 `$` 开头的 id 原样保留：那是模型从当前树里抄来的真实 id，不能动。
 */
export function normalizeOps(
  drafts: MindMapOp[],
  existing: Set<string>,
): { ops: MindMapOp[]; idMap: Record<string, string> } {
  const idMap = new Map<string, string>();
  const used = new Set(existing);

  const realIdOf = (id: string): string => {
    if (!id.startsWith('$')) return id;
    const cached = idMap.get(id);
    if (cached !== undefined) return cached;

    // 和现有节点撞车的可能性极低，但一旦撞上就是 `duplicate_id`，重抽一次最省事。
    let fresh = fullRandomId();
    while (used.has(fresh)) fresh = fullRandomId();
    used.add(fresh);
    idMap.set(id, fresh);
    return fresh;
  };

  const ops = drafts.map((draft): MindMapOp => {
    switch (draft.op) {
      case 'add':
        return { ...draft, id: realIdOf(draft.id), parent_id: realIdOf(draft.parent_id) };
      case 'update':
        // changes 也复制一份：规范化后的批次不该和草案共用同一个可变对象。
        return { ...draft, id: realIdOf(draft.id), changes: { ...draft.changes } };
      case 'remove':
        return { ...draft, id: realIdOf(draft.id) };
      case 'move':
        return { ...draft, id: realIdOf(draft.id), new_parent_id: realIdOf(draft.new_parent_id) };
    }
  });

  return { ops, idMap: Object.fromEntries(idMap) };
}
