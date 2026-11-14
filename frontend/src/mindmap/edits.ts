import type { MindMapContent, MindNode } from '../api/types';

/**
 * 与服务端 Pydantic 约束逐条对应（`backend/app/contracts/mindmap.py`）：
 * 节点 id ≤100、label 1–200、children ≤50、总数 ≤1000、深度 ≤8、title 1–200。
 * 前端先拦一次，省掉一次必然失败的请求；服务端仍是最终裁判（422）。
 */
export const LIMITS = {
  nodes: 1000,
  depth: 8,
  children: 50,
  label: 200,
  note: 2000,
  /** 生成产出的说明（契约 `Node.detail`）。与 `note` 同容量、不同语义。 */
  detail: 2000,
  title: 200,
} as const;

/**
 * AI 生成时标题的软上限（契约 `GENERATED_LABEL_MAX_CHARS`）。
 *
 * 与 `LIMITS.label`（200，契约硬上限、旧图读取口径）是两件事：生成侧要求 16 字，
 * 但旧图里那句 20–30 字的长标题必须照常读、照常存。前端用它只做**提示**（"这个
 * 标题有点长，画布上会折行"），不做校验——把 16 写进校验会让用户改不动旧节点。
 */
export const GENERATED_LABEL_HINT = 16;

/** 复制一棵树：编辑操作都在副本上做，撤销栈里的快照才不会被后续修改带跑。 */
export function cloneTree<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 生成新节点 id。加前缀是为了和模型返回的 id 区分开，避免撞车。 */
export function newId(): string {
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `n_${random}`;
}

export function findNode(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = findNode(child, id);
    if (hit) return hit;
  }
  return null;
}

/** 父节点；根节点返回 null。 */
export function findParent(root: MindNode, id: string): MindNode | null {
  for (const child of root.children) {
    if (child.id === id) return root;
    const hit = findParent(child, id);
    if (hit) return hit;
  }
  return null;
}

export function countNodes(root: MindNode): number {
  return 1 + root.children.reduce((sum, child) => sum + countNodes(child), 0);
}

export function depthOf(root: MindNode): number {
  if (root.children.length === 0) return 1;
  return 1 + Math.max(...root.children.map(depthOf));
}

function replaceChildren(
  node: MindNode,
  parentId: string,
  mapper: (children: MindNode[]) => MindNode[],
): MindNode {
  if (node.id === parentId) return { ...node, children: mapper(node.children) };
  return {
    ...node,
    children: node.children.map(child => replaceChildren(child, parentId, mapper)),
  };
}

/** 改节点文字。返回新的根，原树不动。 */
export function renameNode(root: MindNode, id: string, label: string): MindNode {
  if (root.id === id) return { ...root, label };
  return {
    ...root,
    children: root.children.map(child => renameNode(child, id, label)),
  };
}

/**
 * 改节点正文（契约 v2 的 `note`）。返回新的根，原树不动。
 *
 * 纯空白等价于"没有正文"：后端把纯空白视为 `None`，这里先归一，
 * 免得界面上出现一个 `note: '   '`、保存后又被服务端悄悄改成 null。
 */
export function setNodeNote(root: MindNode, id: string, note: string | null): MindNode {
  if (root.id === id) {
    const trimmed = note?.trim();
    return { ...root, note: trimmed ? trimmed : null };
  }
  return {
    ...root,
    children: root.children.map(child => setNodeNote(child, id, note)),
  };
}

/** 加子节点。返回新树与新节点，方便界面立刻选中它并进入编辑。 */
export function addChild(
  root: MindNode,
  parentId: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  if (!findNode(root, parentId)) throw new Error('父节点不存在');

  const node: MindNode = { id: newId(), label, children: [] };
  return {
    root: replaceChildren(root, parentId, children => [...children, node]),
    node,
  };
}

/**
 * 加兄弟：插在当前节点后面，位置符合直觉（紧接着自己）。
 * 根节点没有兄弟，退化成加子节点。
 */
export function addSibling(
  root: MindNode,
  id: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  const parent = findParent(root, id);
  if (!parent) return addChild(root, root.id, label);

  const node: MindNode = { id: newId(), label, children: [] };
  return {
    root: replaceChildren(root, parent.id, children => {
      const at = children.findIndex(child => child.id === id);
      return [...children.slice(0, at + 1), node, ...children.slice(at + 1)];
    }),
    node,
  };
}

/** 删除节点及其子树。根节点不能删（删了导图就没了）。 */
export function removeNode(root: MindNode, id: string): MindNode {
  if (root.id === id) throw new Error('根节点不能删除');
  return {
    ...root,
    children: root.children
      .filter(child => child.id !== id)
      .map(child => removeNode(child, id)),
  };
}

/** 字数按码点算，和 Pydantic 的 len() 以及画布上的截断保持一致。 */
export function charLength(text: string): number {
  return Array.from(text).length;
}

function shortLabel(label: string): string {
  const chars = Array.from(label.trim());
  return chars.length > 12 ? `${chars.slice(0, 12).join('')}…` : chars.join('');
}

/**
 * 提交前校验整棵树，返回第一条错误文案；没问题返回 null。
 * 文案尽量与服务端一致，避免同一条规则出现两种说法。
 */
export function validateDraft(content: MindMapContent): string | null {
  const title = content.title.trim();
  if (!title) return '标题不能为空';
  if (charLength(title) > LIMITS.title) return `标题最多 ${LIMITS.title} 个字`;

  const seen = new Set<string>();
  const stack: Array<{ node: MindNode; depth: number }> = [{ node: content.root, depth: 1 }];

  while (stack.length > 0) {
    const { node, depth } = stack.pop()!;

    if (seen.has(node.id)) return '节点 ID 必须唯一';
    seen.add(node.id);
    if (seen.size > LIMITS.nodes) return `导图最多 ${LIMITS.nodes} 个节点`;
    if (depth > LIMITS.depth) return `导图最多 ${LIMITS.depth} 层`;

    const label = node.label.trim();
    if (!label) return '节点标题不能为空';
    if (charLength(label) > LIMITS.label) {
      return `节点「${shortLabel(label)}」超过 ${LIMITS.label} 个字`;
    }
    if (node.note && charLength(node.note.trim()) > LIMITS.note) {
      return `节点「${shortLabel(label)}」的正文超过 ${LIMITS.note} 个字`;
    }
    if (node.detail && charLength(node.detail.trim()) > LIMITS.detail) {
      return `节点「${shortLabel(label)}」的详情超过 ${LIMITS.detail} 个字`;
    }
    if (node.children.length > LIMITS.children) {
      return `节点「${shortLabel(label)}」最多 ${LIMITS.children} 个子节点`;
    }

    for (const child of node.children) stack.push({ node: child, depth: depth + 1 });
  }

  return null;
}
