/**
 * 一次运行失败之后，界面上该说什么、下一步能做什么。
 *
 * 从前失败卡片就是后端那句原话（`agentError`），而那句话是写给**写代码的人**看的：
 * 「这次运行超过了时间上限」「模型调用次数已用完」。用户读完既不知道要不要重试，
 * 也不知道该改什么——重试可能一百次都是同一个结果。
 *
 * 所以判据收到这一个纯函数里：后端只负责说清"发生了什么"（`code` / `limit` /
 * `message`），界面负责翻译成"这意味着什么、你现在能做什么"。纯函数的好处是它
 * 被 `failures.spec.ts` 逐条钉住，加一个错误码时不用去翻组件。
 */

/** 卡片上那一排按钮。只有这两种是真能让用户点出结果的。 */
export type FailureActionKind =
  /** 把上一次的请求原样再发一遍 */
  | 'retry'
  /** 把后端原话复制走（发给部署的人 / 贴进工单） */
  | 'copy';

export interface FailureAction {
  kind: FailureActionKind;
  label: string;
  /**
   * 非空＝这个按钮要 disable，且这段文字**必须显示出来**。
   *
   * 只 disable 不解释等于沉默：用户看着一个点不动的「重试」，只能猜是自己点错了。
   * `title` 属性兜不住这件事——被 disable 的按钮在多数浏览器里连悬停提示都不弹。
   */
  disabledReason?: string;
}

export interface FailureGuide {
  /** 一句话说清"发生了什么"，用用户能懂的话。 */
  title: string;
  /** 下一步该做什么。没有可做的事时也要说清"这事不归你管"。 */
  next: string;
  actions: FailureAction[];
  /** 重试是否有意义。false 时「重试」按钮会带着 `disabledReason` 出现在卡片上。 */
  retryable: boolean;
}

export interface FailureInput {
  /** 后端 `error` 帧的 `code`；前端自己合成的失败用下面那几个常量。 */
  code?: string;
  /** 预算类失败的细分原因（`max_seconds` / `max_model_calls` / …）。 */
  limit?: string | null;
  /** 后端原话。**不改写**：卡片把它折在 `<details>` 里给排障的人看。 */
  message?: string;
  /** 这一次的请求还留在 store 里没有（没有就没什么可重试的）。 */
  canRetry?: boolean;
  /** 正在跑的时候重试按钮必须点不动，否则会连开两次。 */
  running?: boolean;
}

/** 前端自己合成的失败（后端不会发这几个 code）。 */
export const CONNECTION_LOST = 'connection_lost';
/**
 * 读流途中抛了网络错，`api/client.ts` 抛的是这个 code。
 *
 * 它与 `CONNECTION_LOST` 是同一种处境："流还在、字不来了"。分两个名字只因为一个
 * 是 store 合成的（EOF 没等到终态帧），一个是客户端抛的（`reader.read()` 失败）。
 * 不给它一条映射的话，这类失败会落到兜底的"这次生成没有成功"——把一句确定的
 * "连接断了、重试有意义"说成含糊的通用失败。
 */
export const STREAM_INTERRUPTED = 'stream_interrupted';
export const NO_CONTENT = 'no_content';
export const INVALID_SOURCES = 'invalid_sources';

const COPY_LABEL = '复制错误信息';

/** 「复制」永远可点：它不依赖任何前置条件，失败时反而最有用。 */
const COPY_ACTION: FailureAction = { kind: 'copy', label: COPY_LABEL };

function retryAction(reason?: string): FailureAction {
  return reason
    ? { kind: 'retry', label: '重试', disabledReason: reason }
    : { kind: 'retry', label: '重试' };
}

/**
 * 「重试」这个按钮该不该点。
 *
 * 三种情况分开说，因为它们对应三种完全不同的处境：这一轮没有可重复的东西、
 * 现在正跑着、以及这类失败重试本来就没用（原因由调用方给）。
 */
function retryBlocker(input: FailureInput, pointless?: string): string | undefined {
  if (pointless) return pointless;
  // 「正在跑」排在「没发出过请求」前面：这两个条件同时成立时（上一轮的失败卡片
  // 还没被新一轮清掉），用户最需要知道的是"再等一下"，而不是"这一轮什么都没发"。
  if (input.running) return '正在跑，等这一轮结束。';
  if (!input.canRetry) return '这一轮还没真正发出请求；改好上面的输入再点生成。';
  return undefined;
}

/**
 * 后端错误码 / 前端合成码 →「发生了什么 + 下一步」。
 *
 * 未知码走兜底分支：宁可只说一句"出错了，可以重试"，也不要猜一个错的解释——
 * 猜错比不说更糟，用户会按错的解释去改设置。
 */
export function guideForFailure(input: FailureInput): FailureGuide {
  return explain(input);
}

function explain(input: FailureInput): FailureGuide {
  const message = (input.message ?? '').trim();
  switch (input.code) {
    case 'model_not_configured':
      // 唯一一个"重试一百次也是同一个结果"的失败，所以它是唯一一个把重试按死的。
      //
      // 指引必须**先指面板**：这个功能本身就是为了"不用登服务器改环境变量再重启"。
      // 只说环境变量会把有面板入口的用户指去找运维，而他要找的那个开关就在页面上。
      return {
        title: '模型还没配置好',
        next:
          '不是网络或额度问题。打开页面右上角的「模型设置」，填好地址、密钥和模型名，'
          + '保存后直接重试，不用重启。没有那个入口说明这个部署没开面板，'
          + '让部署的人配 MINDMAP_LLM_BASE_URL / MINDMAP_LLM_API_KEY / MINDMAP_LLM_MODEL。',
        actions: [retryAction(retryBlocker(input, '配置没改之前，重试还是同一个结果。')), COPY_ACTION],
        retryable: false,
      };
    case 'free_quota_exhausted':
      // 第二个"重试一百次也是同一个结果"的失败，而且它比 `model_not_configured`
      // 更容易被当成网络问题：错误发生在一个正常工作的服务上，只是今天这一格用完了。
      //
      // 指引必须给出**两条都能立刻走的路**，而不是只说"等明天"：填自己的密钥是
      // 当场就能解决的动作，而且正好把这个用户从"蹭公共额度"变成"花自己的钱"，
      // 那对他和对部署方都更好。顺序上先说"明天会自己恢复"，因为那是零成本的那条。
      return {
        title: '今天的免费次数用完了',
        next:
          '免费额度每天零点恢复，明天再来就行。想现在继续，'
          + '打开页面右上角的「模型设置」填自己的密钥——填了就不再受这个限制。',
        actions: [
          retryAction(
            retryBlocker(input, '免费额度今天已经用完了，重试不会多出次数。'),
          ),
          COPY_ACTION,
        ],
        retryable: false,
      };
    case 'auth_required':
      // 门禁回的 401。它只会在一种情况下出现：页面里的游客/会话 cookie 没了——
      // 换了浏览器、清了 Cookie、会话过期（30 天），或者后端换了库。这不是"你操作
      // 错了"，所以指引必须**先给动作**（刷新一下就会拿到新身份），再说额度那句，
      // 否则用户会以为是自己刚才哪个按钮点坏了。
      return {
        title: '这一页的身份过期了',
        next:
          '刷新一下页面就好：刷新时会自动分配一个游客身份（每天有 3 次免费生成），'
          + '想长期保存自己的图就注册一个账号。已经画在画布上的内容刷新后会丢，'
          + '先点「保存」——不过保存同样需要身份，所以这里只能重来。',
        actions: [retryAction(retryBlocker(input, '刷新页面才能拿到新身份，重试还是会被拦。')), COPY_ACTION],
        retryable: false,
      };
    case 'budget_exhausted':
      return explainBudget(input);
    case 'too_many_failures':
      return {
        title: '连续出错太多次，先停下了',
        next:
          '同一个步骤反复失败。先看一眼材料是不是都解析好了，再把目标说小一点重试。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case 'unexpected_finish':
      return {
        title: '回复被截断了',
        next:
          '不是网络断了，是长度上限或内容过滤。把目标说得更具体再试；老是出现就把下面的原话发给维护的人。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case 'generation_failed':
      return {
        title: '模型这一轮出错了',
        next: '多半是限流或临时故障，重试有意义；连着两次都这样，隔一会儿再试。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case 'agent_failed':
      return {
        title: '服务端出错了',
        next: '不是你的操作问题。重试一次通常能过；反复出现就把下面的原话发给维护的人。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case 'no_progress':
      return {
        title: '模型卡住了',
        next: '它连着很多步没有产出新内容。把目标拆成几件小事分别试，比一次说一大段更容易成功。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case 'run_in_progress':
      return {
        title: '这一轮已经在跑了',
        next: '重复提交不会再开第二次。等它结束，或者到页面右侧读它的结果。',
        actions: [retryAction(retryBlocker(input, '等服务端那一轮结束再试。')), COPY_ACTION],
        retryable: false,
      };
    case INVALID_SOURCES:
      return {
        title: '材料还不能用',
        next: message || '有一份材料没有解析好，先把它移出这次请求，或者重新上传一份。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: false,
      };
    case NO_CONTENT:
      return {
        title: '模型这次没有产出导图',
        next: '它回了一段话却没生成图。把主题写得更像"要一张什么结构的图"，或者直接重试一次。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    case CONNECTION_LOST:
    case STREAM_INTERRUPTED:
      return {
        title: '连接中断了',
        next:
          '结果可能不完整。已经长出来的部分如果没落到画布，重试一次；'
          + '重新生成会再花掉同样多的模型额度。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
    default:
      return {
        title: '这次生成没有成功',
        next: message
          ? '可以先重试一次；一直这样就把下面的原话发给维护的人。'
          : '可以先重试一次。',
        actions: [retryAction(retryBlocker(input)), COPY_ACTION],
        retryable: true,
      };
  }
}

/**
 * 预算类失败的细分。
 *
 * 四个 `limit` 对用户是四件不同的事：时间不够（把目标写小）、模型调用次数不够
 * （同上）、工具调用次数不够（同上，但更像"材料太多"）、修复轮数用完（内容有
 * 结构问题）。把它们混成一句"额度用完了"，用户就不知道该缩短目标还是该换材料。
 */
function explainBudget(input: FailureInput): FailureGuide {
  const limit = input.limit ?? '';
  const actions = [retryAction(retryBlocker(input)), COPY_ACTION];
  if (limit === 'max_repairs') {
    return {
      title: '这一轮修不好，先停下了',
      next: '模型交出来的内容反复过不了校验。把目标说得更具体，或者换一份更短的材料。',
      actions,
      retryable: true,
    };
  }
  const what = limit === 'max_tool_calls'
    ? '工具调用次数'
    : limit === 'max_model_calls' ? '模型调用次数' : '时间';
  return {
    title: `${what}不够用`,
    next: `这一次的${what}上限之内没能把图长完。把目标写小一点（比如一次只做一两个分支）再重试。`,
    actions,
    retryable: true,
  };
}
