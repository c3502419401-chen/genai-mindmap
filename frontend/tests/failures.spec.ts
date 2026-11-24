import { describe, expect, it } from 'vitest';

import {
  CONNECTION_LOST,
  guideForFailure,
  INVALID_SOURCES,
  NO_CONTENT,
  STREAM_INTERRUPTED,
} from '../src/mindmap/failures';

/**
 * 失败卡片判据。
 *
 * 这里逐条钉的是"下一步对不对"，不是文案好不好看：同一个 `message` 在不同 `code`
 * 下要给出相反的建议（重试 / 别重试），那正是从前把后端原话直接显示时丢掉的信息。
 */
describe('失败分类', () => {
  it('没配置模型时按死重试，并先指页面上的「模型设置」', () => {
    const guide = guideForFailure({
      code: 'model_not_configured',
      message: '请在后端环境变量中配置模型地址、密钥和模型名称',
      canRetry: true,
    });

    expect(guide.retryable).toBe(false);
    expect(guide.title).toContain('模型还没配置好');
    // 首选路径必须是面板：这个功能本身就是为了"不用登服务器改环境变量再重启"。
    // 只说环境变量会把有面板入口的用户指去找运维。
    expect(guide.next).toContain('模型设置');
    // 面板没开时环境变量仍是兜底，三个名字要留着（那是他唯一能给运维的话）。
    expect(guide.next).toContain('MINDMAP_LLM_BASE_URL');
    expect(guide.next).toContain('MINDMAP_LLM_API_KEY');
    expect(guide.next).toContain('MINDMAP_LLM_MODEL');
    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toBeTruthy();
  });

  it('模型服务出错时重试有意义，按钮可点', () => {
    const guide = guideForFailure({
      code: 'generation_failed',
      message: '模型服务返回 HTTP 503',
      canRetry: true,
    });

    expect(guide.retryable).toBe(true);
    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toBeUndefined();
  });

  it('预算耗尽按 limit 分开说：时间、模型调用、工具调用不是一回事', () => {
    const titles = ['max_seconds', 'max_model_calls', 'max_tool_calls'].map(limit =>
      guideForFailure({ code: 'budget_exhausted', limit, canRetry: true }).title,
    );

    expect(titles[0]).toContain('时间');
    expect(titles[1]).toContain('模型调用次数');
    expect(titles[2]).toContain('工具调用次数');
    // 三个标题必须彼此不同，否则细分 limit 就白做了。
    expect(new Set(titles).size).toBe(3);
  });

  it('修复轮数用完时说的是"这一轮修不好"，不是"额度用完"', () => {
    const guide = guideForFailure({
      code: 'budget_exhausted',
      limit: 'max_repairs',
      canRetry: true,
    });

    expect(guide.title).not.toContain('用完');
    expect(guide.next).toContain('校验');
  });

  it('还没发出去的请求没有可重复的东西，重试按钮要说明这件事', () => {
    const guide = guideForFailure({ code: '', message: '请在画布右上角的输入框里写下主题', canRetry: false });

    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toContain('还没真正发出请求');
  });

  it('正在跑的时候重试也点不动', () => {
    const guide = guideForFailure({ code: 'generation_failed', canRetry: true, running: true });

    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toContain('正在跑');
  });

  it('材料没解析好时下一步是改输入，不是重试一遍', () => {
    const guide = guideForFailure({
      code: INVALID_SOURCES,
      message: '「讲义.pdf」还在解析，等它好了再生成。',
      canRetry: true,
    });

    expect(guide.retryable).toBe(false);
    expect(guide.next).toContain('还在解析');
  });

  it('连接中断与"没产出导图"各自给出不同的下一步', () => {
    const lost = guideForFailure({ code: CONNECTION_LOST, canRetry: true });
    const empty = guideForFailure({ code: NO_CONTENT, canRetry: true });

    expect(lost.title).toContain('连接中断');
    expect(empty.title).toContain('没有产出导图');
    expect(lost.next).not.toBe(empty.next);
  });

  it('读流途中断网（client 抛的 stream_interrupted）也是"连接中断"这一张', () => {
    // 同一件事有两个来源：store 在 EOF 没等到终态帧时合成 connection_lost，
    // client 在 reader.read() 抛错时抛 stream_interrupted。少了这条映射，
    // 后面那种会掉进兜底的"这次生成没有成功"，把一句确定的判断说成含糊的失败。
    const lost = guideForFailure({ code: CONNECTION_LOST, canRetry: true });
    const cut = guideForFailure({ code: STREAM_INTERRUPTED, canRetry: true });

    expect(cut.title).toBe(lost.title);
    expect(cut.next).toBe(lost.next);
    expect(cut.retryable).toBe(true);
  });

  it('免费额度用完时按死重试，并给出"明天恢复"与"填自己的密钥"两条路', () => {
    const guide = guideForFailure({
      code: 'free_quota_exhausted',
      message: '今天的免费生成次数用完了。',
      canRetry: true,
      running: false,
    });

    // 与 `model_not_configured` 一样是"重试一百次同一个结果"——但成因完全不同，
    // 所以标题与下一步都必须另写一份：一个是没配，一个是今天用完了。
    expect(guide.retryable).toBe(false);
    expect(guide.title).toContain('免费次数');
    expect(guide.title).not.toContain('还没配置');
    // 两条路都要给：等明天是零成本的，填密钥是当场能解决的。
    expect(guide.next).toContain('零点');
    expect(guide.next).toContain('模型设置');
    expect(guide.next).toContain('密钥');
    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toContain('用完');
    // 复制仍然在：这张卡片出现时用户往往要去问维护的人。
    expect(guide.actions.some(item => item.kind === 'copy')).toBe(true);
  });

  it('身份过期时先给动作（刷新），再说额度，不假装是操作失误', () => {
    const guide = guideForFailure({
      code: 'auth_required',
      message: '这个操作需要一个身份。',
      canRetry: true,
      running: false,
    });

    // 这一档出现的唯一原因是页面里的 cookie 没了，原样重发还是 401——所以按死重试。
    expect(guide.retryable).toBe(false);
    expect(guide.title).toContain('身份');
    // 先给动作：刷新会领到新身份。
    expect(guide.next).toContain('刷新');
    // 再说额度那条路，否则用户不知道"刷新之后还能不能免费用"。
    expect(guide.next).toContain('游客');
    expect(guide.next).toContain('注册');
    const retry = guide.actions.find(item => item.kind === 'retry');
    expect(retry?.disabledReason).toContain('刷新');
  });

  it('未知错误码走兜底，不猜一个可能错的解释', () => {
    const guide = guideForFailure({ code: 'something_new', message: '后端新加的错误', canRetry: true });

    expect(guide.title).toBe('这次生成没有成功');
    // 原话要能透出来，否则排障时只能看网络面板。
    expect(guide.next).toContain('发给维护的人');
  });

  it('每个分支都给得出一个可点的按钮，不会出现"什么也做不了"的卡片', () => {
    const codes = [
      'model_not_configured',
      'free_quota_exhausted',
      'auth_required',
      'budget_exhausted',
      'too_many_failures',
      'unexpected_finish',
      'generation_failed',
      'agent_failed',
      'no_progress',
      'run_in_progress',
      INVALID_SOURCES,
      NO_CONTENT,
      CONNECTION_LOST,
      '',
    ];

    for (const code of codes) {
      const guide = guideForFailure({ code, canRetry: false });
      expect(guide.title.length, `${code} 缺标题`).toBeGreaterThan(0);
      expect(guide.next.length, `${code} 缺下一步`).toBeGreaterThan(0);
      // 复制永远在：它不依赖任何前置条件，失败时反而最有用。
      const copy = guide.actions.find(item => item.kind === 'copy');
      expect(copy, `${code} 缺复制按钮`).toBeTruthy();
      // 禁用的按钮必须给出原因，否则用户只能猜。
      for (const action of guide.actions) {
        if (action.disabledReason) expect(action.disabledReason.length).toBeGreaterThan(0);
      }
    }
  });
});
