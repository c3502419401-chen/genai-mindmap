import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import type { AgentEvent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

function pushEvents(events: AgentEvent[]) {
  const store = useMindMapStore();
  store.agentEvents = events;
  return store;
}

describe('模型原话', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('没产出导图时把模型的原话与思考摆出来', () => {
    pushEvents([
      { type: 'stage', seq: 1, stage: '准备', detail: 'create 意图，1 个工具可用' },
      {
        type: 'done',
        seq: 2,
        content: null,
        mode: 'model',
        text: '导图大概是：机器学习分监督与无监督……',
        reasoning: '看到 mindmap_generate 了，但直接说更简单。',
      },
    ]);

    const wrapper = mount(AgentPanel);
    const panel = wrapper.text();

    expect(panel).toContain('模型这次只回了文字，没有生成导图');
    expect(panel).toContain('机器学习分监督与无监督');
    expect(panel).toContain('直接说更简单');
  });

  it('有产出时不出现这一段', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '', reasoning: '' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).not.toContain('模型这次只回了文字');
  });

  it('只有正文没有思考时，思考那块不渲染', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '只说了一句话', reasoning: '' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).toContain('只说了一句话');
    expect(wrapper.text()).not.toContain('模型当时在想什么');
  });

  it('正文为空但思考有内容时，也能看出它在想什么', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '', reasoning: '还是直接回答吧' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).toContain('（回复里没有正文）');
    expect(wrapper.text()).toContain('还是直接回答吧');
  });

  /**
   * 深度思考成功的那条路：收尾帧**成功时也带 reasoning**（后端刻意的，那是用户
   * 勾「深度思考」要看的东西）。只按"有思考内容"判的话，导图已经落到画布上了，
   * 面板却宣布"没有调用工具"——用户会以为这次白跑了。
   */
  it('已经产出导图时不说"只回了文字"，但思考过程照旧能看到', () => {
    const store = pushEvents([
      {
        type: 'done',
        seq: 1,
        content: { title: '示例', root: { id: 'r', label: '根', children: [] } },
        mode: 'model',
        text: '',
        reasoning: '先拆成三层，再补每个分支的定义。',
      },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).not.toContain('只回了文字');
    expect(wrapper.text()).not.toContain('没有调用工具');
    expect(wrapper.get('[data-test="run-reasoning"]').text()).toContain('先拆成三层');
    expect(store.agentEvents).toHaveLength(1);
  });

  it('有工具调用记录但没产出时，也不说"没有调用工具"', () => {
    pushEvents([
      {
        type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate_deep',
      },
      {
        type: 'tool_end',
        seq: 2,
        call_id: 'c1',
        name: 'mindmap_generate_deep',
        ok: false,
        result_summary: { code: 'budget', message: '超出预算' },
      },
      { type: 'done', seq: 3, content: null, mode: 'model', text: '我先看了一遍。', reasoning: '再看看。' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).not.toContain('只回了文字');
    expect(wrapper.text()).toContain('超出预算');
  });
});
