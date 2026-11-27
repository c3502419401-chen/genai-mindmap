import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import AliasResolver from '../src/components/AliasResolver.vue';
import * as api from '../src/api/client';
import type { AliasCandidate, AliasPreview } from '../src/api/types';

vi.mock('../src/api/client', () => ({
  startAliasPreview: vi.fn(),
  readAliasPreview: vi.fn(),
  cancelAliasPreview: vi.fn(),
  applyAliasPreview: vi.fn(),
  deleteAlias: vi.fn(),
}));

const evidence = (filename: string, quote: string) => [{
  source_id: filename, filename, start: 0, end: quote.length, quote, kind: 'graph_edge' as const,
}];

const candidates: AliasCandidate[] = [{
  id: '0', alias: '市场监管总局', canonical: '国家市场监督管理总局',
  similarity: 0.6, reason: 'substring', notice: '', selectable: true,
  alias_evidence: evidence('简称.txt', '市场监管总局发布通知。'),
  canonical_evidence: evidence('全称.txt', '国家市场监督管理总局负责监管。'),
}, {
  id: '1', alias: '缺出处', canonical: '缺出处的全称',
  similarity: 0.9, reason: 'embedding', notice: '',
  selectable: false,
  alias_evidence: [], canonical_evidence: evidence('全称.txt', '缺出处的全称。'),
}, {
  // 合称提示：这一对**可选**，但界面必须让人看见"它们可能不是同一个东西"。
  id: '2', alias: '碳中和', canonical: '碳达峰碳中和',
  similarity: 0.5, reason: 'substring',
  notice: '「碳达峰碳中和」可能是「碳中和」与「碳达峰」的合称', selectable: true,
  alias_evidence: evidence('碳中和.txt', '碳中和目标。'),
  canonical_evidence: evidence('双碳.txt', '碳达峰碳中和目标。'),
}];

const running: AliasPreview = {
  job_id: 'job', status: 'running', stage: '正在计算实体向量', error: null, warnings: [], items: [],
};
const done: AliasPreview = {
  ...running, status: 'done', stage: '候选已就绪', items: candidates,
  warnings: ['只有 0 / 2 个实体有向量；其余仅按构词规则比较，可能漏掉候选。'],
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('实体消解预览', () => {
  it('展示两侧原文，默认不选，确认后只应用所选并能逐对撤回', async () => {
    vi.mocked(api.startAliasPreview).mockResolvedValue(running);
    vi.mocked(api.readAliasPreview).mockResolvedValue(done);
    vi.mocked(api.applyAliasPreview).mockResolvedValue({
      relationships_created: 1,
      created: [{ alias: '市场监管总局', canonical: '国家市场监督管理总局' }], skipped: 0,
    });
    vi.mocked(api.deleteAlias).mockResolvedValue({ alias: '市场监管总局', deleted: 1 });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = mount(AliasResolver);

    await wrapper.get('button').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('《简称.txt》');
    expect(wrapper.text()).toContain('市场监管总局发布通知。');
    expect(wrapper.text()).toContain('构词比例 0.60');
    expect(wrapper.text()).toContain('可能漏掉候选');
    expect(wrapper.text()).toContain('不能选择这一对');
    // 合称提示：这一对可选，但"它们可能不是同一个东西"必须在界面上看得见。
    expect(wrapper.text()).toContain('「碳达峰碳中和」可能是「碳中和」与「碳达峰」的合称');
    expect(wrapper.text()).toContain('可能指不同的事物');
    const boxes = wrapper.findAll('input[type="checkbox"]');
    expect((boxes[0].element as HTMLInputElement).checked).toBe(false);
    expect((boxes[1].element as HTMLInputElement).disabled).toBe(true);
    expect(api.applyAliasPreview).not.toHaveBeenCalled();

    await boxes[0].setValue(true);
    const apply = wrapper.findAll('button').find((button) => button.text().includes('确认添加已选'))!;
    await apply.trigger('click');
    await flushPromises();
    expect(window.confirm).toHaveBeenCalledOnce();
    expect(api.applyAliasPreview).toHaveBeenCalledWith({ job_id: 'job', candidate_ids: ['0'] });
    expect(wrapper.text()).toContain('已新增 1 条别名关系');
    await wrapper.findAll('button').find((button) => button.text() === '撤回这一对')!.trigger('click');
    await flushPromises();
    expect(api.deleteAlias).toHaveBeenCalledWith('市场监管总局', '国家市场监督管理总局');
    expect(wrapper.text()).toContain('已撤回');
    expect(wrapper.emitted('changed')).toHaveLength(2);
    wrapper.unmount();
  });

  it('查找中显示真实阶段，取消后不应用候选', async () => {
    vi.mocked(api.startAliasPreview).mockResolvedValue(running);
    vi.mocked(api.readAliasPreview).mockResolvedValue(running);
    vi.mocked(api.cancelAliasPreview).mockResolvedValue({ ...running, status: 'cancelled' });
    const wrapper = mount(AliasResolver);
    await wrapper.get('button').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('正在计算实体向量');
    await wrapper.findAll('button').find((button) => button.text() === '取消查找')!.trigger('click');
    await flushPromises();
    expect(api.cancelAliasPreview).toHaveBeenCalledWith('job');
    expect(wrapper.text()).toContain('未应用任何候选');
    expect(api.applyAliasPreview).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
