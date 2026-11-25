/**
 * 「点节点就地出输入框」的**手势仲裁**：单击之后的那 120ms 里，什么时候不该弹框。
 *
 * ## 为什么需要一个延迟，而不是"点了就弹"
 *
 * 浏览器的一次双击会先发两次 `click` 再发一次 `dblclick`。用户双击一个节点是要
 * **改它的标题**（`MindNode` 的内联输入框），单击是要**对它说一句话**。两者共用
 * "点节点"这个起手式，所以必须等 120ms 看看这一下到底是哪一种：
 *
 * - 双击：第一下 click 排一个定时器，`dblclick` 一到就把定时器撤掉 → 只有编辑框；
 * - 单击：定时器到点，弹意图框。
 *
 * 不延时的版本实测过一次：双击改名的时候，意图框先冒出来、下一帧又被编辑框顶掉，
 * 画面闪一下，而且那半秒里"回车会提交给谁"是含糊的。
 *
 * 120ms 这个数不是调出来的手感，是**落在双击间隔之内、又以肉眼不可察的速度过去**：
 * 常见双击间隔 100–300ms，而 120ms 的单击延迟用户读不出来（远低于 200ms 的可感知
 * 阈值）。真慢的双击（> 120ms）会在定时器到点之后才到 `dblclick`——那种情况由
 * `dblclick` 那一路显式 `closeIntent()` 收尾，框闪一下就消失，与"先弹再被顶掉"
 * 的区别只在于这一下不会被用户用来打字。
 *
 * ## 为什么把判据抽成纯函数
 *
 * 它要回答的是四个彼此无关的问题，而每一个都对应一次**真实的竞态**（用户手比代码
 * 快）。写成一个内联回调的话，这四条只能靠人在浏览器里连着点，而那正是"那天改了一
 * 行、某一种点法坏掉"最不容易被发现的地方。抽出来之后四条各有一条用例。
 */
export const INTENT_DELAY_MS = 120;

export interface IntentGate {
  /** 正在改标题的那个节点（`store.editingId`）。非空 = 双击改名已经接管。 */
  editingId: string | null;
  /** 此刻的主选区（`store.selectedId`）。 */
  selectedId: string | null;
  /** 那个节点此刻还在画布上吗（用户可能在这 120ms 里点了删除）。 */
  exists: boolean;
  /** 这张图能不能开框（树型、且不在跑；见 `store.canEditIntent`）。 */
  canEdit: boolean;
}

/**
 * 定时器到点时，这个框该不该弹。
 *
 * 四条全部成立才弹，缺任何一条都只意味着"这 120ms 里发生了别的事"：
 *
 *  1. `editingId === null` —— 双击改名接管了（两个框都吃回车，不能并存）；
 *  2. `selectedId === id` —— 选中换了人（用户手快，又点了另一个节点）；
 *  3. `exists` —— 节点没了（删掉的节点上不能挂一个"改它"的框）；
 *  4. `canEdit` —— 不是树型 / 正在跑（`runGeneration` 会被 `busy` 挡住，
 *     框开着但提交无效，等于骗用户打完字再告诉他没反应）。
 */
export function openIntentAllowed(id: string, gate: IntentGate): boolean {
  if (gate.editingId !== null) return false;
  if (gate.selectedId !== id) return false;
  if (!gate.exists) return false;
  return gate.canEdit;
}
