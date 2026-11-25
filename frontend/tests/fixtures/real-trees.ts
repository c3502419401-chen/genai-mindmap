/**
 * 真树 fixture：从 `backend/data/mindmaps.db`（以及 `runs.candidate`）里原样导出的结构。
 *
 * 只保留 `id` / `label` / `children` —— 布局与可见性判据只看这三样（节点尺寸恒定
 * `NODE_SIZE`，`detail` / `note` / `source_refs` 不参与几何）。`label` 保留原文，
 * 因为报告里的读数要能对着真实的树看。
 *
 * 为什么不用现造的树：这一轮验收的两个数（可见节点数、`fit-all` 字号）对**分支宽度
 * 与层深**极敏感。真树的形状是「根 4–8 支、叶子堆在最深一层」，与随手写的等宽树
 * 差一个量级，造出来的树量出来的结论不能外推到用户那张图上。
 */
import type { MindMapContent } from '../../src/api/types';

/** 每棵树都是 `MindMapContent`，字段顺序与库里一致（`title` / `root`）。 */
export const REAL_TREES: Record<string, MindMapContent> = {
  // 注意力机制的核心思想（136 节点 / 4 层，来源 c1d30b17-7e80-4c88-81eb-fc57a8fe5089）
  tree136:   {
    "title": "注意力机制的核心思想",
    "root": {
      "id": "n_3fc3cb0125254ecf965fa409772939d0",
      "label": "注意力机制的核心思想",
      "children": [
        {
          "id": "n_20728442566641c399a2edb53c1d5494",
          "label": "从信息过载到选择性关注",
          "children": [
            {
              "id": "n_f72478913af34ebbbc46a61499ddf916",
              "label": "信息过载时需筛选关键输入",
              "children": [
                {
                  "id": "n_4a53950ec6164cc8b01b60987711acc1",
                  "label": "按任务目标判断哪些输入真正相关",
                  "children": [
                    {
                      "id": "n_a27bc54002c94d8ebfec9188d978121a",
                      "label": "先明确任务目标，再据此定义相关性的判断标准",
                      "children": []
                    },
                    {
                      "id": "n_bc6d59e59ab6409084ef554865e1b46e",
                      "label": "用查询向量表示当前任务需求，与各输入逐一比对打分",
                      "children": []
                    },
                    {
                      "id": "n_0b79e80a203f45629329198c97327f62",
                      "label": "按得分高低分配权重，让相关输入获得更多关注",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_c4d66e29555f4ab4818f3626d1eaa6e7",
                  "label": "给不同输入分配不同权重以突出重点",
                  "children": [
                    {
                      "id": "n_bd31d9d94cff49be955e568d0066eae9",
                      "label": "权重用可学习的相似度打分计算得出",
                      "children": []
                    },
                    {
                      "id": "n_cb3ab33337ef4549a95aaad8e8987152",
                      "label": "用归一化把打分转成总和为一的权重",
                      "children": []
                    },
                    {
                      "id": "n_c841ae6ef14d4c9782cd0f7783114337",
                      "label": "加权求和后突出高权重输入的影响",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_51c39a01f30f43daa9a90a205dc50dff",
                  "label": "忽略无关信息以降低计算和认知负担",
                  "children": []
                }
              ]
            },
            {
              "id": "n_b6664fd8198b4a5b92910580933960b3",
              "label": "选择性关注按相关性分配权重",
              "children": [
                {
                  "id": "n_96e6407bd2654a898e720bb394716922",
                  "label": "用查询与键的相似度衡量相关性",
                  "children": [
                    {
                      "id": "n_fd6617147d41427f9e2464188f8af12f",
                      "label": "查询与键做点积得到相似度分数",
                      "children": []
                    },
                    {
                      "id": "n_f36d1244bc6e4452a66ee37a8a12ddb7",
                      "label": "相似度分数经归一化转成注意力权重",
                      "children": []
                    },
                    {
                      "id": "n_39537b5805e3448a806d14e7857f6f7e",
                      "label": "权重越大表示该键对应的信息越相关",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_6ec73cf7d8ba4881917605cafe938060",
                  "label": "按相关性得分分配注意力权重",
                  "children": [
                    {
                      "id": "n_ae158d8bdabd4b81949642b74a0937b7",
                      "label": "用查询与键的相似度计算相关性得分",
                      "children": []
                    },
                    {
                      "id": "n_c1556eba3a944f18a86148087bcd2d70",
                      "label": "对得分归一化得到总和为一的权重",
                      "children": []
                    },
                    {
                      "id": "n_d0f9cb82d15140a7b7f2f1f4b61ffe8b",
                      "label": "用权重对值加权求和得到输出",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_0b7a220f6f16448899f8cea94db115c8",
                  "label": "权重加权汇总得到聚焦后的表示",
                  "children": []
                }
              ]
            },
            {
              "id": "n_4930af2dcd8747bf9acd9d08eaa8f8b0",
              "label": "无关信息被抑制以降低干扰",
              "children": []
            }
          ]
        },
        {
          "id": "n_ab6dfa0a99524cf9870ce56bb2b8ec98",
          "label": "查询、键与值的三元结构",
          "children": [
            {
              "id": "n_4487bb3d009948459ffc85aa7715e9f8",
              "label": "查询与键做相似度计算得到注意力权重",
              "children": [
                {
                  "id": "n_766b9e2af7b84a9bad99bf2f156d4541",
                  "label": "用点积衡量查询与键的匹配程度",
                  "children": [
                    {
                      "id": "n_1084c83a767b437e83731cbe22e10b80",
                      "label": "点积结果越大说明查询与键越匹配",
                      "children": []
                    },
                    {
                      "id": "n_90e32a5f97104cdd8bf685d05f2b9e16",
                      "label": "点积需除以缩放因子防止数值过大",
                      "children": []
                    },
                    {
                      "id": "n_7bcfa4348a7d42fa959cb24548b4570f",
                      "label": "点积计算可写成矩阵乘法批量处理",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_a523c0d572374ed7a332b0b23329cced",
                  "label": "相似度分数经缩放避免数值过大",
                  "children": [
                    {
                      "id": "n_50f5927c19804931bd67bf6b1e6da09e",
                      "label": "缩放因子取键向量维度的平方根",
                      "children": []
                    },
                    {
                      "id": "n_fb96eb60539e4643947eec811aab87e1",
                      "label": "点积随维度增大而方差变大导致梯度消失",
                      "children": []
                    },
                    {
                      "id": "n_61c23a3688d24518b4863490c542c6d0",
                      "label": "缩放后分数经softmax得到平滑权重分布",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_f619339a0f114537871a394c597cd5e4",
                  "label": "分数经归一化后成为注意力权重",
                  "children": [
                    {
                      "id": "n_6aa77d04bbfa4c7cab78387cf836ba0f",
                      "label": "Softmax 把分数转成非负且和为 1 的权重",
                      "children": []
                    },
                    {
                      "id": "n_a1602d0424894ed1bebd4e9719d74097",
                      "label": "除以缩放因子可避免分数过大导致梯度消失",
                      "children": []
                    },
                    {
                      "id": "n_e4369b1466b04052b52c2006a51d7ccf",
                      "label": "归一化后权重用于对值向量加权求和",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_29ba01e8678240239dbb647c8e5f36d5",
              "label": "权重归一化后对值做加权求和",
              "children": [
                {
                  "id": "n_e8b1e746f4094a8ca45dfcf50f67528d",
                  "label": "归一化权重保证各位置贡献可比较",
                  "children": [
                    {
                      "id": "n_3b0cf10b91c34fe289a023db8c03fec2",
                      "label": "Softmax 使权重非负且总和为一",
                      "children": []
                    },
                    {
                      "id": "n_2c035649a1364aff8673350f8c8c6777",
                      "label": "权重同尺度才能横向比较贡献大小",
                      "children": []
                    },
                    {
                      "id": "n_77aaa19bb41d4a45a7a0e7f5ea8e1f17",
                      "label": "归一化后输出是值的凸组合",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_30a6205384594ac59ff1248555192d9d",
                  "label": "加权求和让输出聚焦相关位置的信息",
                  "children": [
                    {
                      "id": "n_1e1011c3feca44c4a2ed6272aa8bb651",
                      "label": "权重大的位置对输出贡献更大",
                      "children": []
                    },
                    {
                      "id": "n_e7bfb91eed7344b389e85e22febe0cd0",
                      "label": "权重小的位置信息被相对抑制",
                      "children": []
                    },
                    {
                      "id": "n_32b6e77b1413495a88e9d213fe4fbf30",
                      "label": "输出是各位置信息的加权混合",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_498e9448f60d440c8fe0750c4a2b865d",
                  "label": "输出是值向量的凸组合，权重和为1",
                  "children": []
                }
              ]
            },
            {
              "id": "n_b41d93f14c1240d2890ee3eac49cb521",
              "label": "三元结构让模型按需检索而非固定读取",
              "children": []
            }
          ]
        },
        {
          "id": "n_072b8d66a6b34c4982d86460842e2517",
          "label": "相似度打分与权重分配",
          "children": [
            {
              "id": "n_ba4dd8eb31f741a1a3dd56f003f928d8",
              "label": "用查询与键的相似度计算注意力分数",
              "children": [
                {
                  "id": "n_7a8bb01bbf184d959e6c5e28abcf0986",
                  "label": "用点积衡量查询与键的匹配程度",
                  "children": [
                    {
                      "id": "n_8e5064426ce64681a0351949b690d49d",
                      "label": "点积结果越大表示查询与键越匹配",
                      "children": []
                    },
                    {
                      "id": "n_234c918b0db943e8b0d9b909fbfac484",
                      "label": "点积需对向量维度做缩放以稳定数值",
                      "children": []
                    },
                    {
                      "id": "n_ee237144e9ad43ef8023efe88a23694a",
                      "label": "点积可用矩阵乘法批量计算所有键",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_a9e4c46ca9d94cc99ee90cf45a75d19d",
                  "label": "相似度分数经缩放避免数值过大",
                  "children": [
                    {
                      "id": "n_379dc11c5d364644abc5b7c24295934b",
                      "label": "缩放因子通常取键向量维度的平方根",
                      "children": []
                    },
                    {
                      "id": "n_4df56dede90c4de0bef29b7385de0b01",
                      "label": "点积随维度增大而方差变大导致分数过大",
                      "children": []
                    },
                    {
                      "id": "n_9016a7d1aa354f1590d6b6789cd5d9e5",
                      "label": "缩放后分数经softmax梯度更稳定",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_05b9d3c621d64072bbaf1ee22b4f4d66",
                  "label": "分数经归一化后作为权重加权求和",
                  "children": [
                    {
                      "id": "n_e41274ba85744bab937b8d97ec51963d",
                      "label": "用 Softmax 把分数转成非负且和为 1 的权重",
                      "children": []
                    },
                    {
                      "id": "n_10713359ca37427999c1161a793ced97",
                      "label": "权重与对应的值向量相乘后逐项相加",
                      "children": []
                    },
                    {
                      "id": "n_ce0647eaa326462b9bfaf09092f739d4",
                      "label": "归一化让权重可比，避免分数尺度影响结果",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_ca608155c9064961a3367ca7ef2ee754",
              "label": "对分数归一化得到总和为一的权重",
              "children": [
                {
                  "id": "n_de8e72851bc1476aa189f73c70d180db",
                  "label": "用 softmax 把分数转成非负且和为 1 的权重",
                  "children": [
                    {
                      "id": "n_b19b5ca74ef945c2ba990ab13dc0e2dc",
                      "label": "softmax 对每个分数取指数，保证输出非负",
                      "children": []
                    },
                    {
                      "id": "n_2e659a6b52244c6aa4ded24db40b6647",
                      "label": "所有指数值相加作分母，实现权重归一化",
                      "children": []
                    },
                    {
                      "id": "n_f83f2e7968a74d2fab4b50ba7b826fa4",
                      "label": "指数运算放大高分项，使权重分布更集中",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_616f149ce49648b78ef140d30a7e0aef",
                  "label": "除以缩放因子防止分数过大导致 softmax 饱和",
                  "children": [
                    {
                      "id": "n_3608f05e062b4199ab58672548a70b65",
                      "label": "点积随维度增大而方差变大",
                      "children": []
                    },
                    {
                      "id": "n_d95e18b645a148d3bdcafe247d935e37",
                      "label": "分数过大会让 softmax 输出接近独热",
                      "children": []
                    },
                    {
                      "id": "n_9eb621da12564a9b9d5f9463e9a6e38c",
                      "label": "除以维度平方根使分数方差回到一",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_7a6d99c28bbb4050b55b2caf2804c194",
                  "label": "归一化后权重用于对值向量加权求和",
                  "children": []
                }
              ]
            },
            {
              "id": "n_0cbbd2aeed904ff8ab7e7d5c0957b6bc",
              "label": "按权重加权求和值向量得到输出",
              "children": [
                {
                  "id": "n_6767be7ba7374581aedea0fbf96b0ef7",
                  "label": "每个输出位置是值向量的加权平均",
                  "children": []
                },
                {
                  "id": "n_c5f66784ca244a4399fd3389fe0e104e",
                  "label": "权重越大，对应值向量对输出贡献越大",
                  "children": []
                },
                {
                  "id": "n_89d5926909aa4344b438c8747763f5bc",
                  "label": "所有输出位置可并行一次矩阵乘法算出",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_77ff9cc27bf34d5299b4deb10accee28",
          "label": "加权求和生成上下文向量",
          "children": [
            {
              "id": "n_26a28baaf5584f48816bfe1762d10985",
              "label": "用注意力权重对各个值向量加权求和",
              "children": [
                {
                  "id": "n_e42e8f600fea496483e66a108aae0aec",
                  "label": "每个值向量乘以对应的注意力权重",
                  "children": []
                },
                {
                  "id": "n_66161986638b4930875ab72778dcd256",
                  "label": "把所有加权后的值向量逐元素相加",
                  "children": []
                },
                {
                  "id": "n_cb4bc2a12ece4d3db71a3cff11394f53",
                  "label": "权重归一化后输出是值向量的凸组合",
                  "children": [
                    {
                      "id": "n_a2fc9d8312b5459fb492691931c3dd73",
                      "label": "归一化权重非负且总和为1",
                      "children": []
                    },
                    {
                      "id": "n_6022e941cd83461d9297ef8bc88d5c19",
                      "label": "输出落在值向量张成的凸包内",
                      "children": []
                    },
                    {
                      "id": "n_abaa7b26eb9d4632a5c2f7273fc572df",
                      "label": "权重集中时输出趋近单个值向量",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_04e2e13114dc4dcc97c8e095326b3fe5",
              "label": "权重反映当前查询对各位置的关注程度",
              "children": []
            },
            {
              "id": "n_f6054be11220467d9518ee03c1296019",
              "label": "加权结果作为当前查询的上下文表示",
              "children": []
            }
          ]
        },
        {
          "id": "n_1456fb7945cb4a2fb8a0ecdbe6b28cd3",
          "label": "多头并行捕捉多类关系",
          "children": [
            {
              "id": "n_11280aeaa487493282a0f1e60dda66b5",
              "label": "每个头用独立投影学习不同子空间",
              "children": [
                {
                  "id": "n_933a409789ca48d39c9ed31a8ec70861",
                  "label": "每个头用独立的线性投影生成自己的查询、键和值",
                  "children": [
                    {
                      "id": "n_a6ec8d4c24c5486890497534ac61bd63",
                      "label": "每个头维护独立的Q、K、V权重矩阵",
                      "children": []
                    },
                    {
                      "id": "n_3acc5cec431b415f9c6675b07aaca7c2",
                      "label": "各头投影维度通常为总维度除以头数",
                      "children": []
                    },
                    {
                      "id": "n_d1a4feca5b464950a688a1e5d8c67a9e",
                      "label": "各头在同一输入上并行计算互不干扰",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_25744acd96ff42feaf47fced6ced6192",
                  "label": "不同头投影到不同子空间以捕捉不同模式",
                  "children": [
                    {
                      "id": "n_80bae57a21214708acd67344b4a75356",
                      "label": "每个头用独立的线性投影生成各自的查询、键和值",
                      "children": []
                    },
                    {
                      "id": "n_96affe106ac5465985c6a30bae8dd64d",
                      "label": "不同头关注输入中不同的特征组合与位置关系",
                      "children": []
                    },
                    {
                      "id": "n_3011595bc232450a96290af84fb11278",
                      "label": "各头输出拼接后再经线性变换融合多种模式",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_1c46ebf1b1d04510897b87a52088fe9e",
                  "label": "各头输出拼接后再经线性变换融合",
                  "children": []
                }
              ]
            },
            {
              "id": "n_729360c5f39a46f081a3a530ac6a85fc",
              "label": "各头并行计算后拼接再线性变换",
              "children": [
                {
                  "id": "n_618d6a2f5a9d4d12b7cb92fd76334094",
                  "label": "每个头独立做缩放点积注意力",
                  "children": [
                    {
                      "id": "n_83717ca450dc46b8b7ae3d2dd8cdf693",
                      "label": "每个头用独立投影矩阵得到自己的Q、K、V",
                      "children": []
                    },
                    {
                      "id": "n_4fbd8554903d4f2485c8ccaa178ecfd7",
                      "label": "每个头内先算Q与K点积再除以缩放因子",
                      "children": []
                    },
                    {
                      "id": "n_b202b6c060c84168930db65da5f660b3",
                      "label": "每个头对缩放后的分数做softmax再加权求和V",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_f968ff4b756c41ff809e7aeb35772188",
                  "label": "各头输出按特征维拼接成完整向量",
                  "children": []
                },
                {
                  "id": "n_e6a615de6c204ac1a5c49071a73847aa",
                  "label": "拼接结果经输出权重矩阵线性变换",
                  "children": [
                    {
                      "id": "n_4103e873e8f4402786c92153ffc79730",
                      "label": "输出权重矩阵把拼接向量映射回模型维度",
                      "children": []
                    },
                    {
                      "id": "n_dde66986a47e42e7a186249f65a0ce60",
                      "label": "线性变换让各头信息按权重融合而非简单堆叠",
                      "children": []
                    },
                    {
                      "id": "n_538e323127f442be93f3bd21122094b4",
                      "label": "该变换与拼接共同构成多头注意力的输出层",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_de484b7ec3c84a55910ebca201b947bc",
              "label": "多头让模型同时关注多种关系模式",
              "children": []
            }
          ]
        },
        {
          "id": "n_3dc550404c784882a57749e015b4332b",
          "label": "自注意力与跨序列对齐",
          "children": [
            {
              "id": "n_4e1d12f2b2d34c008d998825af5992f4",
              "label": "自注意力让序列内部各位置互相计算关联权重",
              "children": [
                {
                  "id": "n_a3450eb2b681477dbc4cb18c7f303400",
                  "label": "每个位置用查询向量与其他位置的键向量算相似度",
                  "children": [
                    {
                      "id": "n_4d1635463c554ff4a07c7915299214ee",
                      "label": "查询向量代表当前位置想找什么信息",
                      "children": []
                    },
                    {
                      "id": "n_0d8188b573a542198c485468ab657fe5",
                      "label": "键向量代表其他位置能提供什么信息",
                      "children": []
                    },
                    {
                      "id": "n_65b439d766a24ca28f6ff4890ab70cca",
                      "label": "相似度用两向量点积计算并得到分数",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_7cc68a642bed4eb2af07851399c2337d",
                  "label": "相似度经归一化后成为该位置对各位置的权重",
                  "children": [
                    {
                      "id": "n_510104531d6240ed9e4414e2ccc238b9",
                      "label": "Softmax 把相似度分数转成总和为 1 的权重",
                      "children": []
                    },
                    {
                      "id": "n_b0c462967deb4c1e9fc2367d95ab6d26",
                      "label": "权重越大表示该位置对当前输出的贡献越高",
                      "children": []
                    },
                    {
                      "id": "n_7bbc2a1c106b4d2aa808755f02feec6e",
                      "label": "归一化后各位置权重可视为一种软性选择",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_b162ce01d6a64beb9a2ebb1d6cb8b55f",
                  "label": "输出是各位置值向量按权重的加权求和",
                  "children": [
                    {
                      "id": "n_5a7fff6244ba42078f73d79503c60e4c",
                      "label": "每个位置的输出等于所有位置值向量的加权平均",
                      "children": []
                    },
                    {
                      "id": "n_02c508850ae045a1ad17fb222d72346d",
                      "label": "权重由查询与键的相似度经归一化得到",
                      "children": []
                    },
                    {
                      "id": "n_3f51ec98eca949a987b28027054bdb6a",
                      "label": "加权求和使每个输出融合全局上下文信息",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_98d43a3d1b8249539d73ea92c2c6fb62",
              "label": "跨序列对齐用一方作查询去匹配另一方的键值",
              "children": [
                {
                  "id": "n_3caf3e7c2bcd404e95522a51c7ca17f9",
                  "label": "用一方序列的表示生成查询向量",
                  "children": [
                    {
                      "id": "n_80df2cc4ac04431fa2d9305306aea9ad",
                      "label": "用编码器每步的隐状态直接充当查询向量",
                      "children": []
                    },
                    {
                      "id": "n_142453c99aca4b7c921a0dec6ed288ac",
                      "label": "把解码器当前隐状态线性变换后得到查询向量",
                      "children": []
                    },
                    {
                      "id": "n_afef0b2d077f4f5387816813092d7391",
                      "label": "查询向量维度需与键向量保持一致才能算相似度",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_6a00b336d8f64ad187f5ae1545766c37",
                  "label": "用另一方序列的表示生成键和值向量",
                  "children": [
                    {
                      "id": "n_a7dca423b30d4fcdb30481f399af5f8e",
                      "label": "键向量由被匹配序列的每个位置表示生成",
                      "children": []
                    },
                    {
                      "id": "n_d7775a28e8674e258e7534be6b902486",
                      "label": "值向量与键向量来自同一序列的表示",
                      "children": []
                    },
                    {
                      "id": "n_492fa417e7524482a26cfbeb63f3844d",
                      "label": "键值成对生成，供查询方逐一比对取用",
                      "children": []
                    }
                  ]
                },
                {
                  "id": "n_be59cd538a7542e6a171e13d8b81a612",
                  "label": "查询与键算相似度再加权汇总值",
                  "children": [
                    {
                      "id": "n_633ec37581704981b9e5c8b640a1fedf",
                      "label": "用点积或缩放点积度量查询与键的相似度",
                      "children": []
                    },
                    {
                      "id": "n_540f69ae9c694800a0d58ad5be0b7e3e",
                      "label": "相似度经归一化后得到各键对应的权重",
                      "children": []
                    },
                    {
                      "id": "n_46f09602e91d41769fc4a5404c34b28e",
                      "label": "用权重对值加权求和得到对齐结果",
                      "children": []
                    }
                  ]
                }
              ]
            },
            {
              "id": "n_bba63b25f8c248638073854501e56c43",
              "label": "对齐权重经归一化后加权汇总对方信息",
              "children": []
            }
          ]
        }
      ]
    }
  },
  // 多智能体思维导图系统的架构与取舍（两段式·图谱）（80 节点 / 3 层，来源 cdce6fa4-b335-4fad-b4ab-857670b826b2）
  tree80:   {
    "title": "多智能体思维导图系统的架构与取舍（两段式·图谱）",
    "root": {
      "id": "root",
      "label": "多智能体思维导图系统的架构与取舍",
      "children": [
        {
          "id": "n_agent",
          "label": "后端可调用 agent 循环",
          "children": [
            {
              "id": "n_agent_loop",
              "label": "自写受限 tool-callin",
              "children": [
                {
                  "id": "n_agent_modules",
                  "label": "四个核心模块分工",
                  "children": []
                },
                {
                  "id": "n_agent_state",
                  "label": "七步状态机",
                  "children": []
                },
                {
                  "id": "n_agent_toolmsg",
                  "label": "工具消息配对与回填",
                  "children": []
                }
              ]
            },
            {
              "id": "n_agent_finish",
              "label": "拒绝截断成功",
              "children": [
                {
                  "id": "n_agent_finish_impl",
                  "label": "ensure_finish_re",
                  "children": []
                },
                {
                  "id": "n_agent_finish_test",
                  "label": "6/6 注入 length 不得",
                  "children": []
                }
              ]
            },
            {
              "id": "n_agent_budget",
              "label": "调用预算与放量门槛",
              "children": [
                {
                  "id": "n_agent_budget_gate",
                  "label": "90 次局部请求精确完成率≥90",
                  "children": []
                },
                {
                  "id": "n_agent_budget_off",
                  "label": "不达标即关开关",
                  "children": []
                }
              ]
            },
            {
              "id": "n_agent_framework",
              "label": "不引入 agent 框架",
              "children": [
                {
                  "id": "n_agent_framework_when",
                  "label": "出现持久恢复再替换实验",
                  "children": []
                },
                {
                  "id": "n_agent_framework_node",
                  "label": "不搬 Node/Vercel A",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_content",
          "label": "MindMapContent 数",
          "children": [
            {
              "id": "n_content_limits",
              "label": "树形约束与校验",
              "children": [
                {
                  "id": "n_content_limits_manual",
                  "label": "人工编辑不跑 MIN_BRANC",
                  "children": []
                },
                {
                  "id": "n_content_limits_id",
                  "label": "新节点 ID 用 uuid4 查",
                  "children": []
                }
              ]
            },
            {
              "id": "n_content_ops",
              "label": "OpBatch 原子应用",
              "children": [
                {
                  "id": "n_content_ops_atomic",
                  "label": "失败整批不提交",
                  "children": []
                },
                {
                  "id": "n_content_ops_rev",
                  "label": "revision=baseRev",
                  "children": []
                }
              ]
            },
            {
              "id": "n_content_relations",
              "label": "relations 与视图属性分",
              "children": [
                {
                  "id": "n_content_relations_sem",
                  "label": "换布局不改语义",
                  "children": []
                },
                {
                  "id": "n_content_relations_src",
                  "label": "source_refs 随导图保",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_rag",
          "label": "RAG 与混合检索",
          "children": [
            {
              "id": "n_rag_hybrid",
              "label": "BM25 与向量混合",
              "children": [
                {
                  "id": "n_rag_hybrid_bm25",
                  "label": "BM25 参数与切词",
                  "children": []
                },
                {
                  "id": "n_rag_hybrid_unknown",
                  "label": "混合是否更准尚无对照",
                  "children": []
                }
              ]
            },
            {
              "id": "n_rag_struct",
              "label": "结构引导的召回",
              "children": [
                {
                  "id": "n_rag_struct_src",
                  "label": "以 source 为输入",
                  "children": []
                },
                {
                  "id": "n_rag_struct_hybrid",
                  "label": "下游接混合检索",
                  "children": []
                }
              ]
            },
            {
              "id": "n_rag_chunk",
              "label": "切块参数与安全规则",
              "children": [
                {
                  "id": "n_rag_chunk_target",
                  "label": "TARGET 靠双向取块生效",
                  "children": []
                },
                {
                  "id": "n_rag_chunk_overlap",
                  "label": "重叠吸附到句首",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_store",
          "label": "图存储选 SQLite",
          "children": [
            {
              "id": "n_store_bench",
              "label": "两跳 0.05ms vs 8.5",
              "children": [
                {
                  "id": "n_store_bench_sqlite",
                  "label": "SQLite 双向邻接",
                  "children": []
                },
                {
                  "id": "n_store_bench_neo4j",
                  "label": "Neo4j 内存与延迟",
                  "children": []
                }
              ]
            },
            {
              "id": "n_store_mem",
              "label": "2C2G 内存账",
              "children": [
                {
                  "id": "n_store_mem_edge",
                  "label": "512m 堆顶到边缘",
                  "children": []
                },
                {
                  "id": "n_store_mem_ok",
                  "label": "256m 堆可共存",
                  "children": []
                }
              ]
            },
            {
              "id": "n_store_risk",
              "label": "已知限制与坑",
              "children": [
                {
                  "id": "n_store_risk_lock",
                  "label": "SQLite 并发写锁争用",
                  "children": []
                },
                {
                  "id": "n_store_risk_tx",
                  "label": "约束走独立事务",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_multi",
          "label": "多模态输入与语音转写",
          "children": [
            {
              "id": "n_multi_asr",
              "label": "语音转写改走云端",
              "children": [
                {
                  "id": "n_multi_asr_why",
                  "label": "本地长输入吃不下",
                  "children": []
                },
                {
                  "id": "n_multi_asr_switch",
                  "label": "两次切换后定百炼",
                  "children": []
                }
              ]
            },
            {
              "id": "n_multi_doc",
              "label": "文档与图片解析",
              "children": [
                {
                  "id": "n_multi_doc_vision",
                  "label": "图片与 PDF 共用视觉调用",
                  "children": []
                },
                {
                  "id": "n_multi_doc_soffice",
                  "label": "soffice 路径可配",
                  "children": []
                }
              ]
            },
            {
              "id": "n_multi_conflict",
              "label": "录音与不做 TLS 冲突",
              "children": []
            }
          ]
        },
        {
          "id": "n_deploy",
          "label": "部署与上线节奏",
          "children": [
            {
              "id": "n_deploy_w1",
              "label": "W1 只上 IP:8080",
              "children": [
                {
                  "id": "n_deploy_w1_tls",
                  "label": "W1 不做 TLS",
                  "children": []
                },
                {
                  "id": "n_deploy_w1_reason",
                  "label": "备案不可控",
                  "children": []
                }
              ]
            },
            {
              "id": "n_deploy_compose",
              "label": "单机 compose 五容器",
              "children": [
                {
                  "id": "n_deploy_compose_pg",
                  "label": "PG 迁移顺延验收后",
                  "children": []
                },
                {
                  "id": "n_deploy_compose_sqlite",
                  "label": "SQLite 兑现含数据库要求",
                  "children": []
                }
              ]
            },
            {
              "id": "n_deploy_branch",
              "label": "main 转生产分支",
              "children": [
                {
                  "id": "n_deploy_branch_pr",
                  "label": "PR 目标一律 dev",
                  "children": []
                },
                {
                  "id": "n_deploy_branch_why",
                  "label": "区分能演示与正在改",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_team",
          "label": "双人分工与协作",
          "children": [
            {
              "id": "n_team_split",
              "label": "A 前端 B 后端",
              "children": [
                {
                  "id": "n_team_split_a",
                  "label": "A 侧交付面",
                  "children": []
                },
                {
                  "id": "n_team_split_b",
                  "label": "B 侧交付面",
                  "children": []
                }
              ]
            },
            {
              "id": "n_team_review",
              "label": "交界改动双人 review",
              "children": [
                {
                  "id": "n_team_review_contract",
                  "label": "契约变更走 checklist",
                  "children": []
                },
                {
                  "id": "n_team_review_collab",
                  "label": "协作功能保留但延后",
                  "children": []
                }
              ]
            },
            {
              "id": "n_team_rhythm",
              "label": "每周节奏与里程碑",
              "children": [
                {
                  "id": "n_team_rhythm_freeze",
                  "label": "11/22 功能冻结",
                  "children": []
                },
                {
                  "id": "n_team_rhythm_buffer",
                  "label": "缓冲期只修 P0",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "n_demo",
          "label": "5 分钟演示脚本",
          "children": [
            {
              "id": "n_demo_gen",
              "label": "0:30–1:10 流式生成",
              "children": [
                {
                  "id": "n_demo_gen_sse",
                  "label": "SSE 逐节点生长",
                  "children": []
                },
                {
                  "id": "n_demo_gen_open",
                  "label": "0:00–0:30 开场讲部署形",
                  "children": []
                }
              ]
            },
            {
              "id": "n_demo_canvas",
              "label": "1:10–2:10 画布编辑",
              "children": [
                {
                  "id": "n_demo_canvas_undo",
                  "label": "整树快照 50 步撤销",
                  "children": []
                },
                {
                  "id": "n_demo_canvas_layout",
                  "label": "布局与语义分离",
                  "children": []
                }
              ]
            }
          ]
        }
      ]
    }
  },
  // 机器学习（95 节点 / 3 层，来源 run-mwvpqvz9-mmkcwzm6）
  tree95:   {
    "title": "机器学习",
    "root": {
      "id": "ml-root",
      "label": "机器学习",
      "children": [
        {
          "id": "g1",
          "label": "基础与定义",
          "children": [
            {
              "id": "g1-1",
              "label": "定义与基本概念",
              "children": [
                {
                  "id": "g1-1-1",
                  "label": "人工智能的一个分支",
                  "children": []
                },
                {
                  "id": "g1-1-2",
                  "label": "从数据自动获取规律并预测",
                  "children": []
                },
                {
                  "id": "g1-1-3",
                  "label": "米切尔的E-T-P定义",
                  "children": []
                },
                {
                  "id": "g1-1-4",
                  "label": "多领域交叉学科",
                  "children": []
                },
                {
                  "id": "g1-1-5",
                  "label": "本质是特征加方法构建模型",
                  "children": []
                }
              ]
            },
            {
              "id": "g1-2",
              "label": "历史与发展脉络",
              "children": [
                {
                  "id": "g1-2-1",
                  "label": "源自人工智能早期探索",
                  "children": []
                },
                {
                  "id": "g1-2-2",
                  "label": "符号方法与早期神经网络",
                  "children": []
                },
                {
                  "id": "g1-2-3",
                  "label": "专家系统主导致统计边缘化",
                  "children": []
                },
                {
                  "id": "g1-2-4",
                  "label": "联结主义与反向传播突破",
                  "children": []
                },
                {
                  "id": "g1-2-5",
                  "label": "1990年代确立为独立领域",
                  "children": []
                }
              ]
            },
            {
              "id": "g1-3",
              "label": "与其他领域的关系",
              "children": [
                {
                  "id": "g1-3-1",
                  "label": "与推断统计学密切相关",
                  "children": []
                },
                {
                  "id": "g1-3-2",
                  "label": "涉及概率论与逼近论等学科",
                  "children": []
                },
                {
                  "id": "g1-3-3",
                  "label": "与模式识别信息检索分流",
                  "children": []
                },
                {
                  "id": "g1-3-4",
                  "label": "借鉴模糊逻辑与概率理论",
                  "children": []
                }
              ]
            },
            {
              "id": "g1-4",
              "label": "理论",
              "children": [
                {
                  "id": "g1-4-1",
                  "label": "核心目标是从经验泛化",
                  "children": []
                },
                {
                  "id": "g1-4-2",
                  "label": "计算学习理论与PAC模型",
                  "children": []
                },
                {
                  "id": "g1-4-3",
                  "label": "偏差-方差分解量化泛化误差",
                  "children": []
                },
                {
                  "id": "g1-4-4",
                  "label": "复杂度匹配与过拟合欠拟合",
                  "children": []
                },
                {
                  "id": "g1-4-5",
                  "label": "时间复杂度与可行性结果",
                  "children": []
                }
              ]
            },
            {
              "id": "g1-5",
              "label": "分类",
              "children": [
                {
                  "id": "g1-5-1",
                  "label": "监督学习需标注训练集",
                  "children": []
                },
                {
                  "id": "g1-5-2",
                  "label": "无监督学习无人工标注",
                  "children": []
                },
                {
                  "id": "g1-5-3",
                  "label": "半监督学习介于两者之间",
                  "children": []
                },
                {
                  "id": "g1-5-4",
                  "label": "强化学习依环境反馈调整行为",
                  "children": []
                },
                {
                  "id": "g1-5-5",
                  "label": "按构建原理分三类模型",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "g2",
          "label": "方法与模型",
          "children": [
            {
              "id": "g2-1",
              "label": "模型与方法",
              "children": [
                {
                  "id": "g2-1-1",
                  "label": "模型选择与训练迭代",
                  "children": []
                },
                {
                  "id": "g2-1-2",
                  "label": "人工神经网络与深度学习",
                  "children": []
                },
                {
                  "id": "g2-1-3",
                  "label": "决策树与随机森林",
                  "children": []
                },
                {
                  "id": "g2-1-4",
                  "label": "支持向量机与核技巧",
                  "children": []
                },
                {
                  "id": "g2-1-5",
                  "label": "回归分析与正则化",
                  "children": []
                },
                {
                  "id": "g2-1-6",
                  "label": "贝叶斯网络与概率图模型",
                  "children": []
                },
                {
                  "id": "g2-1-7",
                  "label": "高斯过程与贝叶斯优化",
                  "children": []
                }
              ]
            },
            {
              "id": "g2-2",
              "label": "量子机器学习",
              "children": [
                {
                  "id": "g2-2-1",
                  "label": "量子计算带来指数级加速",
                  "children": []
                },
                {
                  "id": "g2-2-2",
                  "label": "量子机器学习提升计算精度",
                  "children": []
                },
                {
                  "id": "g2-2-3",
                  "label": "HASM-HHL算法与精度加速",
                  "children": []
                },
                {
                  "id": "g2-2-4",
                  "label": "HHL局限与四套优化方案",
                  "children": []
                }
              ]
            },
            {
              "id": "g2-3",
              "label": "端侧机器学习框架",
              "children": [
                {
                  "id": "g2-3-1",
                  "label": "TFLite轻量跨平台开源框架",
                  "children": []
                },
                {
                  "id": "g2-3-2",
                  "label": "支持安卓iOS与MCU部署",
                  "children": []
                },
                {
                  "id": "g2-3-3",
                  "label": "降低门槛推动端侧普及",
                  "children": []
                },
                {
                  "id": "g2-3-4",
                  "label": "架构实践与工具链",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "g3",
          "label": "工程实践",
          "children": [
            {
              "id": "g3-1",
              "label": "理论与实践",
              "children": [
                {
                  "id": "g3-1-1",
                  "label": "覆盖监督与无监督学习",
                  "children": []
                },
                {
                  "id": "g3-1-2",
                  "label": "模型原理算法与编程示例",
                  "children": []
                },
                {
                  "id": "g3-1-3",
                  "label": "R语言基础与数据处理",
                  "children": []
                },
                {
                  "id": "g3-1-4",
                  "label": "概率统计预备知识",
                  "children": []
                }
              ]
            },
            {
              "id": "g3-2",
              "label": "机器学习及其应用",
              "children": [
                {
                  "id": "g3-2-1",
                  "label": "面向企业需求的算法案例",
                  "children": []
                },
                {
                  "id": "g3-2-2",
                  "label": "十一类基础模型与算法",
                  "children": []
                },
                {
                  "id": "g3-2-3",
                  "label": "案例驱动理解算法使用",
                  "children": []
                },
                {
                  "id": "g3-2-4",
                  "label": "生物信息中的蛋白质应用",
                  "children": []
                }
              ]
            },
            {
              "id": "g3-3",
              "label": "从建模到Web部署",
              "children": [
                {
                  "id": "g3-3-1",
                  "label": "明确问题选择算法工具",
                  "children": []
                },
                {
                  "id": "g3-3-2",
                  "label": "Python基础与数据预处理",
                  "children": []
                },
                {
                  "id": "g3-3-3",
                  "label": "模型存储与Web应用集成",
                  "children": []
                },
                {
                  "id": "g3-3-4",
                  "label": "配套微课视频与源码资源",
                  "children": []
                }
              ]
            },
            {
              "id": "g3-4",
              "label": "Python版教材与微课",
              "children": [
                {
                  "id": "g3-4-1",
                  "label": "理论推导与代码实现并重",
                  "children": []
                },
                {
                  "id": "g3-4-2",
                  "label": "仅依赖NumPy与Matplot",
                  "children": []
                },
                {
                  "id": "g3-4-3",
                  "label": "九章覆盖分类回归与强化学习",
                  "children": []
                },
                {
                  "id": "g3-4-4",
                  "label": "综合实验贯穿各算法",
                  "children": []
                }
              ]
            }
          ]
        },
        {
          "id": "g4",
          "label": "书籍与参考",
          "children": [
            {
              "id": "g4-1",
              "label": "软件",
              "children": [
                {
                  "id": "g4-1-1",
                  "label": "自由开源软件套装",
                  "children": []
                },
                {
                  "id": "g4-1-2",
                  "label": "Python软件库及框架",
                  "children": []
                },
                {
                  "id": "g4-1-3",
                  "label": "R语言函数与工具",
                  "children": []
                }
              ]
            },
            {
              "id": "g4-2",
              "label": "参考文献与参见",
              "children": [
                {
                  "id": "g4-2-1",
                  "label": "引用与来源",
                  "children": []
                },
                {
                  "id": "g4-2-2",
                  "label": "参见条目",
                  "children": []
                },
                {
                  "id": "g4-2-3",
                  "label": "教材参考文献列表",
                  "children": []
                }
              ]
            },
            {
              "id": "g4-3",
              "label": "本书目录",
              "children": [
                {
                  "id": "g4-3-1",
                  "label": "绪论与预备知识",
                  "children": []
                },
                {
                  "id": "g4-3-2",
                  "label": "回归聚类与判别分析",
                  "children": []
                },
                {
                  "id": "g4-3-3",
                  "label": "主成分与支持向量机",
                  "children": []
                },
                {
                  "id": "g4-3-4",
                  "label": "KNN朴素贝叶斯与决策树",
                  "children": []
                },
                {
                  "id": "g4-3-5",
                  "label": "随机森林与神经网络",
                  "children": []
                }
              ]
            },
            {
              "id": "g4-4",
              "label": "内容提要与作者简介",
              "children": [
                {
                  "id": "g4-4-1",
                  "label": "面向高校学生与研究生",
                  "children": []
                },
                {
                  "id": "g4-4-2",
                  "label": "入门教材定位与读者群",
                  "children": []
                },
                {
                  "id": "g4-4-3",
                  "label": "作者李鲁群及教学成果",
                  "children": []
                },
                {
                  "id": "g4-4-4",
                  "label": "出版信息与定价",
                  "children": []
                }
              ]
            },
            {
              "id": "g4-5",
              "label": "关键词",
              "children": [
                {
                  "id": "g4-5-1",
                  "label": "机器学习高等学校教材",
                  "children": []
                },
                {
                  "id": "g4-5-2",
                  "label": "中图分类TP181",
                  "children": []
                },
                {
                  "id": "g4-5-3",
                  "label": "大数据与人工智能技术丛书",
                  "children": []
                }
              ]
            }
          ]
        }
      ]
    }
  },
};
