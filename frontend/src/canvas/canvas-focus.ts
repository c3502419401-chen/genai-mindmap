/**
 * 画布外壳的焦点入口。**一处定义 id，三处用它**（画布、就地输入框、节点内联编辑）。
 *
 * 为什么值得单独一个文件：这套键盘模型靠一条性质撑着——`MindMapCanvas` 的
 * `ownsStructureKeys()` 要求 `document.activeElement` 在画布外壳里。焦点一旦掉到
 * `document.body`，Tab / Enter / Delete / F2 全部静默失效，而**画面上没有任何东西
 * 会显示出错了**（用户只会觉得"按了没反应"）。
 *
 * 三次真实踩坑让这件事必须集中：
 *
 *  1. **节点内联编辑提交之后**：输入框被 `v-if` 卸掉，浏览器把焦点扔回 `body`。
 *     于是"改完标题再按 Enter 加一个兄弟"这条链断在第一步——而连按 Enter 长一串
 *     兄弟正是"纯键盘长节点"的主干（第二轮的实测表里那一行）。
 *  2. **就地输入框关掉之后**：同上，框卸掉，焦点回 `body`。
 *  3. **画布第一次被点**：`focusCanvas()` 本来就该做这件事，但它原先自己写了一遍
 *     `shell.value?.focus()`——组件内的 ref 拿不到"别的组件卸载后该把焦点还给谁"。
 *
 * 找元素用 id 而不是 ref：要还焦点的那一方（`MindNode` / `NodeIntentBox`）不是画布
 * 的父组件，拿不到 `shell` 这个 ref，而它也不该为了这件事把画布实例传下去。
 */
export const CANVAS_SHELL_ID = 'mind-canvas-shell';

/**
 * 把键盘焦点交回画布。返回是否成功（元素不在页面上时返回 `false`，不抛）。
 *
 * `preventScroll`：画布可能比视口大，聚焦外壳时页面跳一下会让人以为图动了。
 */
export function focusCanvasShell(): boolean {
  const shell = document.getElementById(CANVAS_SHELL_ID);
  if (!shell) return false;
  shell.focus({ preventScroll: true });
  return document.activeElement === shell;
}

/**
 * 焦点是不是**掉到无处可去**了（`body` / 没有元素 / 元素已从文档里摘掉）。
 *
 * 这是"该不该把焦点抢回画布"的唯一判据，刻意只认这三种。反例都要放行：
 *
 *  - 焦点还在某个输入框里（用户点了别处开始打字）——抢回来就是坏的；
 *  - 焦点在按钮上（用户点了工具栏，接下来要按空格/回车按那个按钮）——同上；
 *  - 焦点已经在画布里——不用动。
 */
export function focusIsHomeless(): boolean {
  if (typeof document === 'undefined') return false;
  const active = document.activeElement;
  if (!active || active === document.body) return true;
  return !active.isConnected;
}
