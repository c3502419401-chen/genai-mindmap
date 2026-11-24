import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { getModelSettings, saveModelSettings, testModelSettings } from '../api/client';
import type {
  ModelSettings,
  SaveModelSettingsRequest,
  SaveModelSettingsResponse,
  TestModelSettingsRequest,
  TestModelSettingsResponse,
} from '../api/types';

/**
 * 模型设置（`/api/settings/model`）。
 *
 * 这一层只做两件事：缓存"现在生效的是什么"，以及把三个动词的失败翻译成一句能显示
 * 的话。**它不缓存配置本身给后端用**——后端每次调用都自己 `resolve()`，所以这里
 * 存的那份纯粹是给人看的视图。把它当权威会造出一个经典的 bug：页面里显示 A、
 * 后端实际用 B，而两边都"看起来正常"。
 *
 * 三条硬规矩：
 *
 * 1. **明文密钥从不进 store**。接口只回 `api_key_masked`，输入框里用户新打的
 *    那一段只活在面板组件的局部状态里，保存成功就丢掉。
 * 2. **保存只发用户真的改过的字段**。表单初始值来自"生效值"（可能是环境变量），
 *    把整张表单原样 PUT 回去，等于把环境变量里的值搬进库里——之后改环境变量就
 *    不再生效，而用户什么都没做。所以脏字段由面板算，这里只负责发。
 * 3. **`ok=false` 不是异常**。测试连接失败是结论（地址不通 / 密钥被拒 / 模型不
 *    存在），显示成红字；只有 400（三项都没配）才是异常。
 */

/** 这一项来自哪里。与后端 `store.SOURCE_*` 同值。 */
export const SETTING_SOURCE_SAVED = 'saved';
export const SETTING_SOURCE_ENV = 'env';
export const SETTING_SOURCE_UNSET = 'unset';
/**
 * 库里有密钥，但它是给**另一个地址**存下的，所以现在不交出。
 *
 * 与 `unset` 必须分开说：`unset` 的下一句是"去配一把"，而这里的下一句是"你换过
 * 地址，把原来那把再填一次"。混成一句，用户会去供应商后台重开密钥——而库里那把
 * 一直是好的。
 */
export const SETTING_SOURCE_UNBOUND = 'unbound';
/**
 * 生效值来自**本站共享配置**（部署方填的那一份），而不是你自己那行。
 *
 * 与 `saved` 必须分开：看到 `saved` 时你改的就是自己那行；看到 `instance` 时你
 * 改自己那行只是"覆盖"，清掉就回到共享配置。合成一档会让"清除"变成不敢点的
 * 按钮——用户不知道点下去会发生什么。
 */
export const SETTING_SOURCE_INSTANCE = 'instance';
/**
 * 这次生成走的是**免费模型**（部署方那台端点上当前标 `:free` 的那些）。
 *
 * 只出现在 `model_source` / `deep_model_source` 上，不涉及密钥：免费模型也要密钥
 * （不带 key 直接 401），只是那次调用不收费。所以这一档的语义是"用部署方那把钥匙
 * 走不收费的模型"，而不是"不用密钥"。
 *
 * 界面上必须说出来：免费档是慢的（实测约 24.6 tok/s），而且受每日次数限制。用户
 * 看到模型名变成一个不认识的 id 时，得能分辨这是"部署方开了免费档"还是"我配错了"。
 */
export const SETTING_SOURCE_FREE = 'free';
/**
 * 生效密钥是**本站的公共免费密钥**（`MINDMAP_FREE_TIER_API_KEY`），不是任何人的私钥。
 *
 * 与 `instance`/`env` 必须分开，理由只有一个：这是唯一一档**有次数上限**的来源。
 * 用户看到"本站共享配置"会以为可以随便用，看到这一档才该知道"每天几次，用完就没了"。
 * 合并来源会让"这次要不要扣次数"在界面上无从解释。
 */
export const SETTING_SOURCE_PUBLIC = 'public';

/** 来源标签。界面要能分辨"我在面板里设的"与"部署方在环境变量里设的"。 */
export function settingSourceLabel(source: string): string {
  if (source === SETTING_SOURCE_SAVED) return '我的设置';
  if (source === SETTING_SOURCE_INSTANCE) return '本站共享配置';
  if (source === SETTING_SOURCE_PUBLIC) return '本站免费额度';
  if (source === SETTING_SOURCE_FREE) return '免费模型';
  if (source === SETTING_SOURCE_ENV) return '环境变量';
  if (source === SETTING_SOURCE_UNBOUND) return '地址换过，待重填';
  return '未设置';
}

/**
 * 密钥输入框的占位文字。
 *
 * 有已保存的密钥时给打码值（`…abcd`）——用户能认出"是那一把"，而截图或旁人看不出
 * 其余部分。没配时给一个形状示例。
 *
 * `unbound` 单列一档：库里那把密钥不能让输入框显示成"已保存"，否则用户看着占位
 * 里那把（属于旧地址的）以为配好了，而生成一路 401。
 */
export function apiKeyPlaceholder(settings: ModelSettings | null): string {
  if (!settings) return 'sk-…';
  if (settings.api_key_source === SETTING_SOURCE_UNBOUND) {
    return '地址换过了，请重新粘贴密钥';
  }
  if (settings.api_key_set) return `${settings.api_key_masked}（留空＝不改）`;
  return 'sk-…';
}

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<ModelSettings | null>(null);
  /**
   * 初值 `false`：面板是**点了才开**的，第一次打开时确实还没有任何结论。
   *
   * 这与 `usePrefsStore.loading` 初值 `true` 的理由不同——那份是页面一挂载就拉，
   * 首帧就必须避免说"没有偏好"。这里没人请求过，就是"还没拉"。
   */
  const loading = ref(false);
  /** 拉过一次没有。用它区分"未配置"与"还没问过"——前者是结论，后者不是。 */
  const loaded = ref(false);
  const saving = ref(false);
  const testing = ref(false);
  const error = ref('');
  /** 上一次测试连接的结论；面板把它显示成一行，`null` = 还没测过。 */
  const result = ref<TestModelSettingsResponse | null>(null);
  /**
   * 保存后后端要提醒的一句话，例如"地址改了，之前那次测试的结论不再成立"。
   *
   * 与 `error` 分开：这不是失败，保存是成功的，只是有件事用户该知道。混进 `error`
   * 会让界面上出现"红色的成功"。
   */
  const notice = ref('');

  /** 三项齐了没有。判据在后端，这里只是把它的答案拿出来。 */
  const configured = computed(() => settings.value?.configured ?? false);
  /**
   * 部署方把 `MINDMAP_LLM_MODE` 设成了 `demo`。
   *
   * 那时**面板里配什么都白配**：agent 会走占位内容，视觉提取直接拒绝。界面必须
   * 说出来，否则用户会以为是密钥或地址填错了。
   */
  const isDemoMode = computed(() => (settings.value?.llm_mode ?? '') === 'demo');

  function apply(next: ModelSettings): void {
    settings.value = next;
    loaded.value = true;
  }

  /** 拉当前生效配置。重复调用会重拉——面板的「刷新」与每次打开都该看到最新的。 */
  async function load(): Promise<void> {
    loading.value = true;
    error.value = '';
    try {
      apply(await getModelSettings());
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '读取模型设置失败。';
    } finally {
      loading.value = false;
    }
  }

  /**
   * 保存。**只发被改动过的字段**（见文件顶部第 2 条）。
   *
   * 成功后用响应就地更新，不重拉：重拉会多一次"刚存的值还没到界面"的窗口，而
   * 这个面板的整个卖点就是"改完立刻是对的"。
   */
  async function save(body: SaveModelSettingsRequest): Promise<SaveModelSettingsResponse | null> {
    saving.value = true;
    error.value = '';
    try {
      const saved = await saveModelSettings(body);
      apply(saved);
      notice.value = saved.notice ?? '';
      return saved;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '保存模型设置失败。';
      return null;
    } finally {
      saving.value = false;
    }
  }

  /**
   * 测一次连接。返回值可能是 `null`——那表示请求本身没发出去（后端 400/网络断）。
   *
   * 与 `save` 分开的 `testing` 标志：两个按钮各自转自己的圈，不会互相把对方禁掉。
   */
  async function test(
    body: TestModelSettingsRequest = {},
  ): Promise<TestModelSettingsResponse | null> {
    testing.value = true;
    error.value = '';
    result.value = null;
    // 测完就不再是"没测过"了：那句"地址改了、建议测一次"的提醒到此为止。
    // 否则用户点完测试，提醒还挂在旁边，读起来像测试没生效。
    notice.value = '';
    try {
      result.value = await testModelSettings(body);
      return result.value;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '测试连接失败。';
      return null;
    } finally {
      testing.value = false;
    }
  }

  /** 面板上"清除密钥"：显式走 `clear_api_key`，绝不靠提交空串。 */
  function clearApiKey(): Promise<SaveModelSettingsResponse | null> {
    return save({ clear_api_key: true });
  }

  function clearResult(): void {
    result.value = null;
  }

  return {
    settings,
    loading,
    loaded,
    saving,
    testing,
    error,
    result,
    notice,
    configured,
    isDemoMode,
    load,
    save,
    test,
    clearApiKey,
    clearResult,
  };
});
