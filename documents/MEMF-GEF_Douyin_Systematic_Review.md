# MEMF-GEF (Douyin)：基于异构关系图神经网络的多模态实体融合直播销量预测——系统综述

> **摘要**：直播电商销量预测是电子商务智能决策的核心问题。现有工作 MEMF 提出了"先实体内多模态融合，再跨实体融合"的范式，在淘宝直播场景取得显著效果，但存在两大盲区：(1) 排除了观众交互信号，(2) 跨实体融合依赖全连接自注意力，无法编码社交关系拓扑。另一方面，社交直播平台（以抖音为代表）具有独特的实时交互数据生态，但受限于隐私与 API 约束，仅能获取直播中公开可见的数据。本文以 MEMF [Xu et al., DSS 2024]、MGL [Ektefaie et al., Nat. Mach. Intell. 2023]、以及《Multimodal Learning with Transformers: A Survey》[Xu et al., TPAMI 2023] 为理论基石，提出 **MEMF-GEF (Graph-Enhanced Entity Fusion)**——一种面向抖音直播的多模态异构关系图神经网络框架。核心贡献包括：(1) 重新定义"仅直播中可获取数据"约束下的实体与模态体系；(2) 将 MEMF 的 Multi-Entity Transformer 升级为异构关系图 Transformer (HGT)，以类型化关系边替代全连接自注意力；(3) 引入时序切片节点建模直播动态；(4) 提出完整的图结构、融合策略、训练方案与评估设计。

---

## 目录

1. [引言](#1-引言)
2. [理论基础与相关工作](#2-理论基础与相关工作)
3. [问题定义与抖音数据约束](#3-问题定义与抖音数据约束)
4. [MEMF-GEF 完整架构](#4-memf-gef-完整架构)
5. [图增强实体融合层](#5-图增强实体融合层)
6. [动态时序建模](#6-动态时序建模)
7. [训练策略与损失函数](#7-训练策略与损失函数)
8. [实验设计与消融方案](#8-实验设计与消融方案)
9. [讨论与未来方向](#9-讨论与未来方向)
10. [参考文献](#10-参考文献)

---

## 1. 引言

### 1.1 研究背景

直播电商（Live Streaming Commerce）已成为全球电子商务增长最快的赛道之一。以抖音（Douyin/TikTok）为代表的社交直播平台将内容消费与商品交易深度融合，2024 年中国直播电商市场规模突破 4.9 万亿元。在直播场景中，实时准确地预测商品销量对库存管理、供应链调度和营销资源配置具有关键价值。

然而，直播销量预测面临独特挑战：(1) **多实体交互**——主播、商品、直播间、观众四类实体之间存在复杂的显式和隐式关系；(2) **多模态异质性**——文本（标题、弹幕）、图像（商品图、封面）、音频（主播语音）、视频（直播画面）、数值（价格、观看人数）等模态统计特性差异巨大；(3) **时序动态性**——观众数量、互动频率、购买热度随直播进程剧烈波动；(4) **数据可获取性约束**——与淘宝后台全量数据不同，抖音直播仅能获取公开可见的实时数据流。

### 1.2 核心研究问题

> 在仅使用抖音直播中可获取的公开数据（主播资料、商品信息、直播间信息、弹幕、实时观看人数、点赞数、礼物通知、购买通知、直播音视频片段）的约束下，如何构建一个融合多实体、多模态、时序动态信号的异构关系图神经网络，实现直播商品的销量预测？

### 1.3 本文贡献

1. **问题重定义**：首次在"仅直播中可获取数据"的强约束下定义直播电商多模态多实体销量预测问题，明确与 MEMF 等后台数据依赖型工作的本质区别。
2. **架构创新**：提出 MEMF-GEF，将全连接 Multi-Entity Transformer 升级为**五类节点、十二种类型边**的异构关系图 Transformer (HGT)，从几何拓扑学视角（Xu et al., TPAMI 2023）重新理解实体融合。
3. **时序整合**：引入时序切片节点 (Time-Slice Nodes)，将直播动态信号（观看量曲线、互动热力）编码为图中结构，使模型同时捕获静态实体关系与动态演化模式。
4. **数据可行**：所有输入信号均来自抖音直播公开可见数据流，无需后台用户画像、交易数据库或跨会话追踪，具备现实可复现性。

---

## 2. 理论基础与相关工作

### 2.1 MEMF：多实体多模态融合框架

MEMF [1] 针对淘宝直播场景提出三层 Transformer 融合架构：

- **Multimodal Transformer**：对主播、直播间实体的文本/图像/数值三模态做实体内自注意力融合。
- **Multimodal QuadTransformer**：对 M 件商品做两阶段融合——阶段一（Intra-commodity）每商品内模态融合，阶段二（Inter-commodity）商品间融合。
- **Multi-Entity Transformer**：对 5 类特征（主播/商品/直播间/视频/音频）做全连接自注意力。

核心发现：实体内模态相关性 >> 跨实体同模态相关性，因此"先实体内多模态融合，再跨实体融合"优于传统"同模态先行聚合"范式。但 MEMF 存在两个根本局限：① 论文明确排除了观众数据（"audience data is excluded due to its real-time generation and uncontrollable nature"）；② Multi-Entity Transformer 用全连接自注意力建模实体关系，存在噪音连接与拓扑盲区。

### 2.2 MGL：多模态图学习蓝图

MGL [2] 将多模态图学习统一为四组件框架：

```
Component 1: Identifying Entities——实体识别与投影
Component 2: Uncovering Topology——拓扑发现与邻接矩阵构建
Component 3: Propagating Information——消息传递
Component 4: Mixing Representations——表示混合
```

MGL 按应用场景将多模态图分为三类：Image-Intensive Graphs (IIG)、Language-Intensive Graphs (LIG)、Knowledge-Intensive Graphs (KIG)。直播电商场景涉及图像（商品图/封面）、语言（标题/弹幕）、知识（社交关系/共现模式）的混合，属于跨类别复合图。

MGL 提供了四个关键设计策略可供借鉴：

| 策略 | MGL 来源 | MEMF-GEF 映射 |
|------|---------|--------------|
| 双通道消息传递 (Intra/Inter-class) | HOI 任务中 Human Graph + Interaction Graph 独立传播 | 观众内部 GAT + 跨实体 HGT 双通道 |
| 类型感知消息传递 | 依赖解析图中不同依存类型用独立参数 | HGT 中每种边类型的独立注意力缩放因子 μ_r |
| 多尺度层级建模 | 蛋白质原子→残基→超像素层级聚合 | 个体观众→观众群体→实体向量的两级聚合 |
| Interaction Block 门控融合 | ABSA 中注意力加权融合 intra/inter-aspect | Cross-Type Gate 学习型门控 |

### 2.3 Transformer 的多模态学习理论基础

Xu et al. [3] 对 Transformer 的多模态学习进行了系统性综述，提出了若干对 MEMF-GEF 设计具有直接指导意义的洞见：

#### 几何拓扑学视角

> "Self-attention can be treated as a graph style modelling, which models the input sequence (both uni-modal and multimodal) as a fully-connected graph. Specifically, self-attention models the embedding of arbitrary tokens from an arbitrary modality as a graph node." [3, §III]

这一视角为 MEMF-GEF 提供了理论基础：全连接自注意力本质上是全连接图神经网络，而引入结构化的稀疏图（基于领域先验的边类型）等价于引入归纳偏置，理论上可以降低样本复杂度并提升泛化能力。

#### 六种跨模态交互范式

[3, §III-C2, Table II] 系统归纳了 Transformer 实现跨模态交互的六种变体：

| 范式 | 公式特征 | MEMF-GEF 中的应用 |
|------|---------|-----------------|
| Early Summation | $Z ← Tf(αZ^(A) ⊕ βZ^(B))$ | 实体特征编码器内的模态嵌入加和 |
| Early Concatenation | $Z ← Tf(C(Z^(A), Z^(B)))$ | 多源文本 token 的拼接编码 |
| Hierarchical (multi→one) | $Z ← Tf₃(C(Tf₁(Zᴬ), Tf₂(Zᴮ)))$ | 实体编码→图融合的层级结构 |
| Hierarchical (one→multi) | 共享单流→分支双流 | InterBERT 式保留 uni-modal 独立性 |
| Cross-Attention | $Z^(A) ← MHSA(Q^B, K^A, V^A)$ | HGT 中 Query 来自目标节点类型，Key/Value 来自源节点类型 |
| Cross-Attn → Concat | 交叉注意力后再拼接融合 | 双通道输出经 Cross-Type Gate 融合 |

MEMF-GEF 主要采用 **Hierarchical (multi→one) + Cross-Attention + Cross-Attn→Concat** 的组合策略：实体编码器独立处理各实体（multi-stream），HGT 层执行跨实体消息传递（cross-attention 范式），双通道输出通过 Cross-Type Gate 拼接融合（cross-attn→concat）。

#### 七项技术挑战

[3, §V] 提出了多模态 Transformer 的七项挑战：Fusion（融合）、Alignment（对齐）、Transferability（可迁移性）、Efficiency（效率）、Robustness（鲁棒性）、Universalness（通用性）、Interpretability（可解释性）。MEMF-GEF 的设计在这些维度上的考虑如下：

| 挑战 | MEMF-GEF 的设计回应 |
|------|-------------------|
| Fusion | 三级融合：early（实体内模态加和）→ middle（HGT 消息传递）→ late（类型感知门控） |
| Alignment | 通过共现统计与 Jaccard 相似度隐式对齐观众-商品-主播关系 |
| Efficiency | K=100 群体聚类压缩图规模；时序切片 T=10~20 可控 |
| Robustness | 时序切片冗余编码 + Dropout + 关系边 Top-K 稀疏化 |
| Interpretability | 关系类型缩放因子 μ_r 可视化 + 消融实验揭示各实体/边贡献 |

### 2.4 图神经网络相关工作

#### HGT：异构关系图 Transformer

HGT (Heterogeneous Graph Transformer) [4] 是 MEMF-GEF 跨实体消息传递的核心基座。HGT 的关键设计包括：

- **类型感知线性投影**：每种节点类型 τ 有独立的 $Q/K/V$ 投影矩阵 $W^Q_τ, W^K_τ, W^V_τ$
- **异构互注意力**：每种关系类型 $ϕ(e)$ 有可学习缩放因子 $μ_{ϕ(e)}$
- **相对时间编码**（本工作未使用，替换为时序切片节点显式建模动态）

#### GAT：图注意力网络

GAT [5] 通过注意力机制为不同邻居分配不同权重，是观众群体内部消息传递（Intra-Audience GAT）的基础模块。其注意力系数：

$$\alpha_{ij} = \frac{\exp(\text{LeakyReLU}(\mathbf{a}^T [W h_i \| W h_j]))}{\sum_{k \in \mathcal{N}_i} \exp(\text{LeakyReLU}(\mathbf{a}^T [W h_i \| W h_k]))}$$

### 2.5 与 MEMF 原论文的遗产关系

| MEMF 模块 | MEMF-GEF 中的处理 |
|----------|-----------------|
| Multimodal Transformer (Anchor/Room) | **保留**，用于主播和直播间实体的实体内多模态融合 |
| Multimodal QuadTransformer (Commodity) | **保留**，用于商品实体内+商品间融合，输出池化后作为单实体向量 |
| Multi-Entity Transformer (全连接自注意力) | **升级**为 HGT + Intra-GAT 双通道，用类型化关系边替代全局自注意力 |
| Video/Audio 特征提取 (VideoMAE, Wav2Vec 2.0) | **保留**，直接复用预训练提取器 |
| DNN Predictor | **保留**，输入从 5 个实体向量扩展为 4+N_ts+N_au 个融合后向量 |

---

## 3. 问题定义与抖音数据约束

### 3.1 形式化定义

给定一场抖音直播的直播前静态信息与直播中动态信号，预测直播中各商品的总销量。

**输入**：

- **主播实体 A**：主播个人资料文本 $T_A$、头像/封面图像 $I_A$、数值特征 $N_A$（粉丝数、历史场观均值、带货口碑分）
- **商品实体集 C = {C₁, ..., C_M}**：每件商品的标题文本 $T_C$、商品图 $I_C$、数值特征 $N_C$（价格、折扣、历史销量、好评率）
- **直播间实体 R**：直播间标题 $T_R$、背景图 $I_R$、数值特征 $N_R$（开播时段、预计时长）
- **直播音视频片段 V, S**：直播画面视频帧序列、音频片段序列
- **观众交互流**：弹幕文本序列 D = {d₁, ..., d_K}、实时观看人数序列 W = {w₁, ..., w_T}、点赞数 L_t、礼物通知 G_t、购买通知 P_t

**输出**：每件商品 $C_i$ 的预测销量 $ŷ_i ∈ ℝ⁺$

**关键约束**：所有输入信号必须在抖音直播中公开可获取，不使用后台用户画像、交易数据库、跨会话用户追踪、用户-用户社交图谱。

### 3.2 抖音 vs 淘宝直播数据差异

| 维度 | 淘宝直播 (MEMF 场景) | 抖音直播 (本工作场景) |
|------|----------------------|---------------------|
| 数据来源 | 第三方数据商（惠豚）后台全量数据 | 直播公开 API + 实时爬取 |
| 用户级数据 | 可获取用户画像和购买历史 | **不可获取**（隐私约束） |
| 社交关系 | 不可获取 | 可获公开关注数、粉丝数（聚合级） |
| 弹幕 | 淘宝直播有弹幕但 MEMF 未使用 | **核心数据源**（实时文本流） |
| 观看人数 | 后台精确值 | 前端展示人数（近似值） |
| 送礼 | 不适用（淘宝无礼物系统） | 公开礼物通知（含送礼者昵称、礼物类型） |
| 购买通知 | 无 | 公开滚动通知"XXX 正在购买" |
| 跨平台 | 单平台 | 可能涉及跨平台（抖音+头条+西瓜） |

### 3.3 数据可获取性约束对设计的影响

由于无法获取用户级细粒度数据，MEMF-GEF 原有的"个体观众节点 + 社交边"设计需做以下调整：

| 原有设计 | 抖音约束下的调整 |
|---------|----------------|
| 个体观众节点 u_i（用户画像 + 行为特征） | **合并**为观众群体节点 g_j，基于弹幕/行为模式聚类 |
| follow / gift / purchase 边（精确用户-主播关系） | **降级**为聚合级统计边（如"该群体向主播送礼的比例"） |
| co_view / similar_audience 边（用户共现/相似矩阵） | **替换**为弹幕语义共现边（"同时提及某商品"的观众群体关联） |
| 跨会话用户追踪 | **不可行**，改为单场直播内时间窗口行为聚类 |

这并不意味着建模能力的退化——恰恰相反，这一约束迫使模型从"依赖精确用户画像的身份匹配"转向"基于公开行为信号的模式识别"，更加符合现实社交直播平台的应用场景，也是一种更具泛化性的问题设定。

---

## 4. MEMF-GEF 完整架构

### 4.1 总体设计理念

MEMF-GEF 的设计遵循三条核心原则：

1. **几何拓扑学先验**（源自 [3]）：自注意力本质是全连接图。将没有先验的隐式注意力替换为**基于领域知识的结构化稀疏图**（类型化边），注入归纳偏置，降低样本复杂度。
2. **MGL 四组件映射**（源自 [2]）：将直播电商的实体识别、拓扑发现、消息传播、表示混合严格映射到 MGL 的四组件框架。
3. **数据可行性优先**：每个输入信号必须在抖音直播中可获取，不使用任何后台不可见数据。

### 4.2 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                    DNN Predictor (RELU + Dropout)                │
│                    ŷ = MLP(z_An ⊕ z_Co* ⊕ z_Rm ⊕ z_Au ⊕ z_Ts)   │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │  Z_final
┌─────────────────────────────────────────────────────────────────┐
│           Graph-enhanced Entity Fusion Layer (L=4)               │
│                                                                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│  │ z_An    │ │ z_Co*   │ │ z_Rm    │ │ z_Au*   │ │ z_Ts    │   │
│  └────▲────┘ └────▲────┘ └────▲────┘ └────▲────┘ └────▲────┘   │
│       │            │            │            │            │       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │           Heterogeneous Graph Transformer (HGT)             │  │
│  │    L layers × {HGT Conv + Intra-Group GAT + Cross-Type     │  │
│  │                Gate + FFN + LayerNorm + Residual}           │  │
│  └────────────────────────────────────────────────────────────┘  │
│       ▲            ▲            ▲            ▲            ▲       │
└───────┼────────────┼────────────┼────────────┼────────────┼──────┘
        │            │            │            │            │
  ┌─────┴──┐   ┌─────┴───┐  ┌────┴───┐  ┌────┴────┐  ┌───┴──────┐
  │ Anchor │   │Commodity│  │  Room  │  │Audience │  │Time-Slice│
  │ Entity │   │ Entity  │  │ Entity │  │ Entity  │  │  Entity  │
  │ Encoder│   │ Encoder │  │ Encoder│  │ Encoder │  │  Encoder │
  └────────┘   └─────────┘  └────────┘  └─────────┘  └──────────┘
       │            │            │            │            │
  Text/Image   Text/Image   Text/Image   弹幕/行为    观看量/点赞
     /Num        /Num         /Num       聚类/语义    /礼物/购买
                                         (K=100)    (T=10~20 slices)
```

### 4.3 实体节点体系（MGL Component 1）

| 节点类型 | 符号 | 数量 | 特征来源 | 输出维度 |
|---------|------|:---:|---------|:---:|
| **Anchor** 主播 | ν_An | 1 | Multimodal Transformer → 文本/图像/数值融合 | D̂=512 |
| **Commodity** 商品 | ν_Co | M | QuadTransformer → 商品内+商品间融合 → Pool | D̂=512 |
| **Room** 直播间 | ν_Rm | 1 | Multimodal Transformer → 文本/图像/数值融合 | D̂=512 |
| **Audience-Group** 观众群体 | ν_Au | K=100 | 弹幕语义聚类 + 行为特征聚合 → MLP | D̂=512 |
| **Time-Slice** 时序切片 | ν_Ts | T=10~20 | 该时间窗内聚合指标 → MLP + Transformer | D̂=512 |

**关键设计决策——时序切片节点**：MEMF 原论文仅在直播前做预测，忽略直播中的动态变化。抖音直播中实时观看人数、互动热度的时序演化携带强烈的销量信号。引入 Time-Slice 节点作为图中的"时序锚点"，将每一段时间窗内的聚合动态信息编码为节点，通过 `ts_audience_state`、`ts_commodity_attention` 等边与观众群体和商品节点相连。

#### Anchor Encoder（主播实体编码器）

遵循 MEMF 的 Multimodal Transformer 设计：

```
输入: T_A (BERT 768d) + I_A (ResNet50 2048d→512d) + N_A (MLP 64d)
过程: Token embedding = T_A ⊕ PosEmb; Image embedding = I_A; Num embedding = N_A
     Z_An^(0) = concat([CLS], T_A_tokens, IMG_token, NUM_token)
     Z_An = Multimodal_Transformer(Z_An^(0))  # L=2 层自注意力
输出: z_An = Z_An[CLS] ∈ R^512
```

#### Commodity Encoder（商品实体编码器）

遵循 MEMF 的 QuadTransformer 设计，两阶段：

```
阶段一 (Intra-commodity): 对每件商品 i
    F_i ∈ R^{(L_t+L_i+L_n) × D̂}  ← 文本/图像/数值模态拼接
    X_i = SelfAttention(F_i)  # 商品内跨模态融合

阶段二 (Inter-commodity): 跨 M 件商品
    X̂_i = MeanPool(X_i)  # 每商品取模态平均
    X = concat(X̂_1, ..., X̂_M) ∈ R^{M×D̂}
    Z_Co = SelfAttention(X)  # 商品间关系建模

输出: z_Co* = MeanPool(Z_Co) ∈ R^512  # 商品群整体表示
       同时保留 z_Co_i = Z_Co[i] ∈ R^512  # 每件商品的独立表示（用于细粒度边）
```

#### Room Encoder（直播间实体编码器）

与 Anchor Encoder 同构，使用 Multimodal Transformer：

```
输入: T_R (BERT 768d) + I_R (ResNet50→512d) + N_R (MLP 64d)
输出: z_Rm = Multimodal_Transformer(T_R, I_R, N_R)[CLS] ∈ R^512
```

#### Audience-Group Encoder（观众群体编码器）——关键创新

**MGL Component 1 映射**：观众侧的实体识别与投影。

由于无法获取个体用户身份，采用**双层聚合**策略：

```
第一层：弹幕级特征
  每条弹幕 d_k:
    h_k_text = BERT(d_k.content) ∈ R^768         # 弹幕文本语义
    h_k_ctx  = MLP([timestamp, like_count])      # 弹幕上下文特征
    h_k = concat(h_k_text, h_k_ctx) ∈ R^832

第二层：时间窗口聚合 → 群体发现
  - 将直播等分为 T 个时间窗口
  - 每窗口 w_t 内聚合所有弹幕的 h_k，取该窗口的用户列表（基于弹幕发送者昵称，非用户ID）
  - 使用 K-Means(K=100) 对"昵称-行为向量"聚类：
      行为向量: [发言频率, 平均弹幕长度, 情感得分, 提及商品频率, 
                 是否送礼, 是否在购买通知中出现, 活跃窗口数]
  - 每群体 g_j 聚合: h_j_group = MeanPool({h_k | user ∈ g_j})

第三层：群体节点特征投影
  z_Au_j^(0) = MLP_Au(concat(h_j_group, [群体大小, 活跃度均值, 情感均值, 送礼率])) ∈ R^512
```

#### Time-Slice Encoder（时序切片编码器）——新增

```
对时间窗口 w_t (t=1,...,T):
  - 聚合数值: [观看人数 w_t, 观看增量 Δw_t, 点赞数 l_t, 礼物数 g_t, 
               购买通知数 p_t, 弹幕数 d_t, 弹幕情感均值 s_t]
  - 窗口内提到最多的 Top-3 商品 ID embedding
  - 数值特征过 MLP: h_ts_num = MLP_Ts([w_t, Δw_t, l_t, g_t, p_t, d_t, s_t]) ∈ R^256
  - 融合商品注意力: h_ts = h_ts_num + MeanPool(Top3_commodity_embeddings)
  - 时间位置编码: PE(t) = sin/cos encoding
  z_Ts_t^(0) = h_ts + PE(t) ∈ R^512
```

同时，对 T 个切片节点做一次轻量 Temporal Self-Attention，注入序列上下文：

$$Z_{Ts} = \text{MHSA}([z_{Ts\_1}^{(0)}, ..., z_{Ts\_T}^{(0)}]) \in \mathbb{R}^{T \times 512}$$

### 4.4 异构关系图拓扑定义（MGL Component 2）

基于抖音直播中可获取的数据，定义**十二种类型化关系边**（比原 MEMF-GEF 报告的九种新增三类，替换四种不可获取的）：

#### 边类型定义

| # | 边类型 | 源→目标 | 语义 | 抖音数据来源 | 构建方式 |
|:--:|--------|--------|------|------------|---------|
| 1 | `hosts` | ν_An → ν_Rm | 主播主持直播间 | 结构化数据 | 1:1 硬连接 |
| 2 | `showcases` | ν_Rm → ν_Co | 直播间展示商品 | 商品橱窗列表 | 1:N 硬连接 |
| 3 | `comments_anchor` | ν_Au → ν_An | 观众群体评论主播 | 弹幕中@主播或提及主播名 | 文本提及率 ≥ 阈值 |
| 4 | `comments_commodity` | ν_Au → ν_Co | 观众群体讨论商品 | 弹幕中提及商品关键词 | 文本匹配 + 语义相似度 Top-K |
| 5 | `gifts_to` | ν_Au → ν_An | 观众群体向主播送礼 | 礼物通知（昵称+礼物类型） | 群体内送礼人数占比 ≥ 阈值 |
| 6 | `purchase_intent` | ν_Au → ν_Co | 观众群体购买意向 | 购买通知 + 弹幕购买意愿词 | 购买通知中昵称匹配 + "下单/买了"等词检测 |
| 7 | `likes_room` | ν_Au → ν_Rm | 观众群体点赞直播间 | 点赞增量在窗口内的群体归属 | 基于发言活跃度比例分配 |
| 8 | `watches` | ν_Au → ν_Rm | 观众群体观看 | 直播观看人数在群体间分配 | 按群体活跃度比例分配 |
| 9 | `co_mention` | ν_Co → ν_Co | 商品被共同提及 | 弹幕中同时提及两商品 | Jaccard 共现 Top-K |
| 10 | `audience_similarity` | ν_Au → ν_Au | 观众群体行为相似 | 行为向量余弦相似度 | Top-K (K=5) |
| 11 | `ts_audience_state` | ν_Ts → ν_Au | 时刻 t 的观众群体状态 | 该窗口内群体行为聚合 | 时间窗内行为向量拼接 |
| 12 | `ts_commodity_attention` | ν_Ts → ν_Co | 时刻 t 的商品受关注度 | 该窗口内商品提及/购买频率 | 窗口内提及+购买通知归一化 |

#### 边类型关系模式表

```
Relation Schema (s_type, t_type):
  hosts:                   (Anchor, Room)
  showcases:               (Room, Commodity)
  comments_anchor:         (Audience, Anchor)
  comments_commodity:      (Audience, Commodity)
  gifts_to:                (Audience, Anchor)
  purchase_intent:         (Audience, Commodity)
  likes_room:              (Audience, Room)
  watches:                 (Audience, Room)
  co_mention:              (Commodity, Commodity)
  audience_similarity:     (Audience, Audience)
  ts_audience_state:       (TimeSlice, Audience)
  ts_commodity_attention:  (TimeSlice, Commodity)
```

#### 图规模分析

| 节点类型 | 数量 | 节点度（平均） |
|---------|:---:|:---:|
| Anchor | 1 | ~2+K+T |
| Commodity | M (~20-50) | ~1+K+5+T |
| Room | 1 | ~2+K |
| Audience-Group | K=100 | ~3+5+2T |
| Time-Slice | T=10~20 | ~(K+M) |

以 M=50, K=100, T=10 计：总节点约 171，总边约 2,050——规模极小，支持批处理训练和多层 HGT。

---

## 5. 图增强实体融合层

**MGL Components 3 & 4 映射**：基于异构关系图执行消息传递（Component 3）和表示混合（Component 4）。

### 5.1 层结构

共 **L=4 层**，每层包含三个子模块 + FFN + 残差连接：

```
Layer l 输入: H^(l-1) = {h_An, h_Co, h_Rm, h_Au, h_Ts}
                         │
          ┌──────────────┼──────────────────┐
          ▼              ▼                  ▼
    ┌──────────┐  ┌──────────────┐  ┌──────────────┐
    │HGT Conv  │  │Intra-Group   │  │Cross-Type    │
    │跨实体类型 │  │GAT           │  │Gate          │
    │消息传递  │  │观众/商品内部 │  │门控融合      │
    │12种关系边│  │同质图传播    │  │自适应权重    │
    └────┬─────┘  └──────┬───────┘  └──────┬───────┘
         │               │                  │
         └───────────────┼──────────────────┘
                         ▼
              FeedForward + LayerNorm + Residual
                         │
                         ▼
              H^(l) = {h'_An, h'_Co, h'_Rm, h'_Au, h'_Ts}
```

### 5.2 子模块一：HGT Conv（异构跨实体消息传递）

**理论基础**：Xu et al. [3] 指出，"cross-modal interactions are essentially processed by self-attention and its variants"。HGT Conv 将融合从同质全连接自注意力推广为异质关系型注意力——每种关系类型对应一种独特的交叉模态交互模式。

#### Step 1: 类型感知投影

对每种节点类型 τ ∈ {Anchor, Commodity, Room, Audience, TimeSlice}，学习独立的 Q/K/V 投影矩阵：

$$Q^\tau = W_Q^\tau \cdot h^\tau, \quad K^\tau = W_K^\tau \cdot h^\tau, \quad V^\tau = W_V^\tau \cdot h^\tau$$

其中 $$W_Q^\tau, W_K^\tau, W_V^\tau \in \mathbb{R}^{D̂ \times D̂}$$。

#### Step 2: 多头异构注意力

对于头 head_i（共 H=8 个头，每头 d_k = D̂/H = 64），边类型 r = ϕ(e)：

$$\text{Attn}_i(s, e, t) = \text{softmax}_{s \in \mathcal{N}_r(t)}\left( \frac{Q_{s,i} \cdot K_{t,i}^T}{\sqrt{d_k}} \cdot \mu_r \right)$$

其中 **μ_r ∈ ℝ 是可学习的边类型缩放因子**，使模型自动学习不同关系的重要性。预期 `purchase_intent` 的 μ_r >> `co_mention` 的 μ_r。

#### Step 3: 类型感知聚合

按关系类型分组聚合后拼接：

$$h'_t = \mathop{\|}_{r \in \mathcal{R}, t \in \text{target}(r)} \left( \sum_{s \in \mathcal{N}_r(t)} \alpha_{s,t}^r \cdot W_V^{\tau(s)} h_s \right) \cdot W_{out}^{\tau(t)}$$

```python
# HGT Conv 伪代码
def HGT_Conv(h_dict, edge_index_dict, rel_schema):
    """
    h_dict: Dict[node_type -> Tensor[N_t, D]]
    edge_index_dict: Dict[rel_name -> (src, tgt)]
    rel_schema: Dict[rel_name -> (src_type, tgt_type)]
    """
    H = 8  # 头数
    d_k = 512 // H
    
    h_new = {t: [] for t in node_types}
    
    for r_name, (s_type, t_type) in rel_schema.items():
        src, tgt = edge_index_dict[r_name]
        E = len(src)  # 边数
        
        # 类型感知投影
        Q = W_Q[s_type](h_dict[s_type])[src].view(E, H, d_k)
        K = W_K[t_type](h_dict[t_type])[tgt].view(E, H, d_k)
        V = W_V[s_type](h_dict[s_type])[src].view(E, H, d_k)
        
        # 异构注意力 + 关系类型缩放
        attn = (Q * K).sum(dim=-1) / sqrt(d_k)  # [E, H]
        attn = attn * mu[r_name]  # 可学习关系缩放
        
        # 目标节点内 softmax
        attn = scatter_softmax(attn, tgt, dim=0)  # [E, H]
        
        # 加权聚合 + 按目标节点求和
        msg = (attn.unsqueeze(-1) * V)  # [E, H, d_k]
        msg = scatter_sum(msg, tgt, dim=0, 
                          dim_size=num_nodes[t_type])  # [N_t, H, d_k]
        msg = msg.view(num_nodes[t_type], H * d_k)  # [N_t, D]
        
        h_new[t_type].append(msg)
    
    # 拼接所有关系类型的消息
    for t in node_types:
        if h_new[t]:
            h_new[t] = concat(h_new[t], dim=-1) @ W_out[t]
        else:
            h_new[t] = h_dict[t]  # 无入边的节点保持原表示
    
    return h_new
```

### 5.3 子模块二：Intra-Group 同质图传播

**理论基础**：借鉴 MGL 中 HOI 的 intra-class message passing 和 ABSA 的 intra-aspect 建模——异质消息传递捕获跨类型交互，但同类型节点间的网络效应需要独立建模。

**两个独立的同质子图**：

#### Intra-Audience GAT

对观众群体节点 {ν_Au}，内部边为 `audience_similarity`：

$$\alpha_{ij}^{Au} = \frac{\exp(\text{LeakyReLU}(\mathbf{a}_{Au}^T [W_{Au} h_i \| W_{Au} h_j]))}{\sum_{k \in \mathcal{N}_i^{Au}} \exp(\text{LeakyReLU}(\mathbf{a}_{Au}^T [W_{Au} h_i \| W_{Au} h_k]))}$$

$$h_i^{intra\_Au} = \sigma\left( \sum_{j \in \mathcal{N}_i^{Au}} \alpha_{ij}^{Au} \cdot W_{Au} h_j \right)$$

生物学隐喻：观众群体间的信息级联和从众效应——高互动群体影响行为相似的低互动群体。

#### Intra-Commodity GAT

对商品节点 {ν_Co}，内部边为 `co_mention`：

$$\alpha_{ij}^{Co} = \frac{\exp(\text{LeakyReLU}(\mathbf{a}_{Co}^T [W_{Co} h_i \| W_{Co} h_j]))}{\sum_{k \in \mathcal{N}_i^{Co}} \exp(\text{LeakyReLU}(\mathbf{a}_{Co}^T [W_{Co} h_i \| W_{Co} h_k]))}$$

$$h_i^{intra\_Co} = \sigma\left( \sum_{j \in \mathcal{N}_i^{Co}} \alpha_{ij}^{Co} \cdot W_{Co} h_j \right)$$

经济学隐喻：商品间的互补/替代效应——经常被同时提及的商品在销量上存在联动关系。

对于 Anchor、Room、TimeSlice 节点，它们均为单节点或节点数少，不设 Intra-Group 传播。

### 5.4 子模块三：Cross-Type Gate（跨类型门控融合）

**理论基础**：借鉴 [3] 中 cross-attention to concatenation 的范式——两种信息通道的输出需要可学习的拼接融合，而非固定加权。

对每个实体节点，学习一个门控比率 γ，自适应融合 HGT 通道与 Intra-Group 通道：

$$\gamma_t = \sigma\left(W_{gate}^{\tau(t)} \cdot [h_t^{HGT} \| h_t^{intra}] + b_{gate}^{\tau(t)}\right)$$

$$h_t^{fused} = \gamma_t \odot h_t^{HGT} + (1 - \gamma_t) \odot h_t^{intra}$$

其中 ⊙ 表示逐元素乘法。对于无 Intra-Group 通道的节点类型（Anchor, Room, TimeSlice），直接取 h_t^{HGT}。

门控机制使得模型可以在以下维度自适应：
- 当跨实体交互信号强时（如 purchase_intent 边密集），γ → 1，侧重 HGT
- 当同类型节点间网络效应主导时（如观众从众、商品替代），γ → 0，侧重 Intra-Group

### 5.5 下游预测器

L 层 HGT 后，取各实体类型的最终表示，进行全局池化并预测：

```
实体级表示:
  z_An  = h_An^(L)                                      ∈ R^512
  z_Co* = MeanPool([h_Co_1^(L), ..., h_Co_M^(L)])      ∈ R^512
  z_Rm  = h_Rm^(L)                                      ∈ R^512
  z_Au  = AttnPool([h_Au_1^(L), ..., h_Au_K^(L)])       ∈ R^512
  z_Ts  = TemporalPool([h_Ts_1^(L), ..., h_Ts_T^(L)])   ∈ R^512

全局表示:
  Z_final = z_An ⊕ z_Co* ⊕ z_Rm ⊕ z_Au ⊕ z_Ts           ∈ R^2560

预测:
  ŷ = MLP(Z_final)        # 2层: 2560→512→1 (ReLU + Dropout 0.3)
```

注意力池化 (AttnPool) 使模型关注高价值观众群体；TemporalPool 为带时间衰减因子的加权平均，近期切片权重更高。

---

## 6. 动态时序建模

### 6.1 时序切片的双重角色

Time-Slice 节点在图中扮演两个互补角色：

1. **作为图中节点**：与 Audience 和 Commodity 节点建立边（ts_audience_state, ts_commodity_attention），将时序动态作为结构性先验注入消息传递。
2. **作为时序编码器**：T 个切片间执行轻量自注意力，捕获跨时间段的演化模式（如"观看量在 30 min 时达峰，随后下降"）。

### 6.2 时间窗口划分策略

抖音直播平均时长约 2-4 小时。建议方案：

| 策略 | 窗口数 | 每窗时长 | 适用场景 |
|------|:---:|------|---------|
| 均匀切分 | T=10 | ~15 min (2.5h 直播) | 默认方案 |
| 对数切分 | T=12 | 近密远疏（前密后疏） | 开播前 30 min 信息密度高 |
| 自适应切分 | 变长 T | 基于弹幕/互动拐点检测 | 事件驱动型直播（闪购/秒杀） |

### 6.3 时序信号的图传播路径

观众在时间 t 的行为如何影响商品销量预测：

```
TimeSlice_t ──(ts_audience_state)──→ Audience_j
                                         │
                    ┌────────────────────┘
                    ▼
              Audience_j ──(purchase_intent)──→ Commodity_i
                    │
                    ▼
              Audience_j ──(comments_commodity)──→ Commodity_i
```

弹幕情感从正转负时，对应的 TimeSlice 节点携带负向信号，通过 Audience 群体节点传导至 Commodity 节点，影响最终销量预测。

---

## 7. 训练策略与损失函数

### 7.1 损失函数

销量预测为回归任务，主损失采用 Huber Loss（对离群值鲁棒）：

$$\mathcal{L}_{huber} = \begin{cases} \frac{1}{2}(y - \hat{y})^2 & \text{if } |y - \hat{y}| \leq \delta \\ \delta(|y - \hat{y}| - \frac{1}{2}\delta) & \text{otherwise} \end{cases}$$

其中 δ=1.0。

附加辅助损失——关系类型重要性正则化：

$$\mathcal{L}_{rel} = \sum_{r \in \mathcal{R}} |\mu_r - \frac{1}{|\mathcal{R}|}|$$

鼓励模型不将所有注意力集中在少数关系类型上。

总损失：

$$\mathcal{L} = \mathcal{L}_{huber} + \lambda_{rel} \mathcal{L}_{rel}, \quad \lambda_{rel} = 0.01$$

### 7.2 训练配置

| 超参数 | 值 | 说明 |
|--------|-----|------|
| HGT 层数 L | 4 | 平衡感受野与过平滑 |
| 注意力头数 H | 8 | 每头 d_k=64 |
| 隐层维度 D̂ | 512 | 所有实体统一维度 |
| Audience 群体数 K | 100 | 平衡粒度与稀疏性 |
| 时序切片数 T | 12 | 对数切分 |
| 优化器 | AdamW | lr=1e-4, weight_decay=1e-4 |
| 学习率调度 | CosineAnnealing | T_max=100, eta_min=1e-6 |
| Batch size | 16 | 每批 16 场直播 |
| Epochs | 200 | Early stopping patience=20 |
| Dropout | 0.3 | 应用于 Attention 和 FFN |
| 梯度裁剪 | 1.0 | — |

### 7.3 特征提取器选择

| 模态 | 提取器 | 预训练数据 | 是否微调 |
|------|--------|----------|:---:|
| 文本（中文） | BERT-wwm-ext (RoBERTa-zh) | 中文维基+新闻 | ✅ 微调最后 2 层 |
| 图像 | ResNet50 (ImageNet) | ImageNet-1K | ✅ 微调全层 |
| 视频片段 | VideoMAE-base | Kinetics-400 | ❌ 冻结 |
| 音频片段 | Wav2Vec 2.0-base (zh) | 中文语音 | ❌ 冻结 |
| 数值特征 | MLP (3层: 64→128→64) | 从头训练 | ✅ |
| 弹幕情感 | SnowNLP / 微调 BERT | 中文情感 | ✅ |

---

## 8. 实验设计与消融方案

### 8.1 评估指标

| 指标 | 全称 | 公式 | 关注点 |
|------|------|------|--------|
| MAE | Mean Absolute Error | $$\frac{1}{N}\sum |y_i - \hat{y}_i|$$ | 绝对误差 |
| MSE | Mean Squared Error | $$\frac{1}{N}\sum (y_i - \hat{y}_i)^2$$ | 对大误差惩罚 |
| MAPE | Mean Absolute Percentage Error | $$\frac{1}{N}\sum \|\frac{y_i - \hat{y}_i}{y_i}\|$$ | 相对误差 |
| R² | Coefficient of Determination | $$1 - \frac{\sum (y_i - \hat{y}_i)^2}{\sum (y_i - \bar{y})^2}$$ | 拟合优度 |

### 8.2 基线模型

| 基线 | 类型 | 说明 |
|------|------|------|
| XGBoost | 传统 ML | 数值特征 + TF-IDF 文本 |
| MLP | 深度学习 | 拼接所有特征过 3 层 MLP |
| LSTM-Attention | 时序 DL | LSTM 编码时序特征 + 注意力 |
| MEMF (5-entity) | SOTA | 原论文全模块，但无观众数据 |
| MEMF + 弹幕均值 | 增强基线 | MEMF + 弹幕文本均值作为附加特征 |
| HGT (w/o Intra-Group) | MEMF-GEF 消融 | 仅 HGT，无 Intra-Group GAT |
| HGT (w/o TimeSlice) | MEMF-GEF 消融 | 移除 TimeSlice 节点 |
| **MEMF-GEF (Full)** | **本工作** | 完整模型 |

### 8.3 消融实验组

#### 组一：实体消融

| 移除实体 | 评估意义 |
|---------|---------|
| w/o Audience | 验证观众信号的边际贡献 |
| w/o TimeSlice | 验证时序结构的必要性 |
| w/o Anchor | 验证主播特征的贡献 |
| w/o Commodity | 验证商品特征的贡献（预期贡献最大，参考 MEMF 结论） |
| w/o Room | 验证直播间特征的贡献 |

#### 组二：图结构消融

| 移除边集 | 对应的关系类型 | 评估意义 |
|---------|-------------|---------|
| w/o purchase_intent | 边 #6 | 购买通知信号的贡献 |
| w/o gifts_to | 边 #5 | 礼物信号的贡献 |
| w/o comments_* | 边 #3, #4 | 弹幕文本信号的贡献 |
| w/o co_mention | 边 #9 | 商品共现建模的必要性 |
| w/o ts_* 边 | 边 #11, #12 | 时序-实体连接的贡献 |
| 随机图 (同规模随机边) | — | 验证图结构先验 vs 随机结构 |

#### 组三：模块消融

| 配置 | 评估意义 |
|------|---------|
| Full HGT → 全连接 Self-Attn | HGT 类型感知 vs 通用注意力 |
| Full Cross-Type Gate → 固定 0.5 | 学习型门控 vs 固定平均融合 |
| Full K=100 → K=20 / K=500 | 群体粒度的敏感性 |
| Full T=12 → T=1 / T=24 | 时序分辨率的敏感性 |
| Full L=4 → L=1 / L=8 | 图传播深度的敏感性 |

### 8.4 与 MEMF 原论文的对比注意事项

MEMF 在淘宝直播数据（2030 场直播，121 位主播，177,405 件商品）上训练，其 MAE 基线为 0.3175（归一化后）。抖音直播数据分布差异显著（更长尾、更多冲动消费），不建议直接做跨数据集数值比较。应关注：

1. 相对基线的提升幅度（Δ%）
2. 各实体的相对贡献排序（是否仍为 Commodity > Video > Room > Anchor > Audio）
3. 观众实体和时序实体带来的增量贡献（MEMF 未建模）

---

## 9. 讨论与未来方向

### 9.1 本工作的局限性

1. **单直播独立建模**：每场直播作为独立样本，未建模同一主播跨场次的长期品牌效应和粉丝积累。《Multimodal Learning with Transformers》[3] 指出的 Transferability 挑战——"how to transfer models across different datasets and applications"——在此表现为跨场次知识迁移问题。

2. **弹幕语义分析的噪音**：抖音弹幕包含大量网络用语、表情符号和反讽表达，常规情感分析工具可能失效。这一局限性对应 [3] 提出的 Robustness 挑战。

3. **购买通知的样本偏差**：抖音仅展示部分购买通知（平台选择性推送），非全量随机样本，可能引入系统性偏差。

4. **时序切片固定切分**：可能错过秒杀、闪购等短时爆发事件。这对应 [3] 讨论的 Alignment 挑战——"fine-grained semantic alignment across modalities"。

5. **跨品类泛化**：服饰、美妆、3C 等品类的购买决策逻辑差异大，统一模型可能在各品类间存在性能方差。

### 9.2 未来研究方向

1. **因果建模**（对应 [3] 的 Interpretability 挑战）：当前 HGT 的关系类型缩放因子 μ_r 部分可解释，但尚不能回答因果问题——"弹幕数量多是销量高的原因还是结果？"引入 do-operator 和反事实推理是重要方向。

2. **跨场次主播嵌入**：为每个主播学习长期嵌入，允许跨直播场次的知识迁移（[3] 的 Transferability）。

3. **多模态预训练 + 微调**（对应 [3] §IV-A）：利用大量无标签直播数据进行 BERT 式多模态预训练，设计跨模态掩码预训练任务（Masked Danmaku Prediction, Masked Commodity Modeling），缓解标注数据稀缺问题。

4. **自适应时序切分**：基于弹幕密度和互动频率的变点检测，动态确定时序切片边界，捕获突发营销事件。

5. **跨平台泛化**（对应 [3] 的 Universalness 挑战）：将 MEMF-GEF 迁移至 TikTok（海外版）、快手、视频号等平台，验证框架的通用性。

---

## 10. 参考文献

[1] G. Xu, M. Ren, Z. Wang, and G. Li, "MEMF: Multi-entity multimodal fusion framework for sales prediction in live streaming commerce," *Decision Support Systems*, vol. 184, 2024.

[2] Y. Ektefaie, G. Dasoulas, A. Noori, M. Farhat, and M. Zitnik, "Multimodal learning with graphs," *Nature Machine Intelligence*, vol. 5, pp. 340–350, 2023.

[3] P. Xu, X. Zhu, and D. A. Clifton, "Multimodal learning with transformers: A survey," *IEEE Transactions on Pattern Analysis and Machine Intelligence*, vol. 45, no. 10, pp. 12113–12132, 2023.

[4] Z. Hu, Y. Dong, K. Wang, and Y. Sun, "Heterogeneous graph transformer," in *Proc. The Web Conference (WWW)*, 2020, pp. 2704–2710.

[5] P. Veličković, G. Cucurull, A. Casanova, A. Romero, P. Liò, and Y. Bengio, "Graph attention networks," in *Proc. International Conference on Learning Representations (ICLR)*, 2018.

[6] A. Vaswani et al., "Attention is all you need," in *Proc. Advances in Neural Information Processing Systems (NeurIPS)*, 2017, pp. 5998–6008.

[7] C. Sun, A. Myers, C. Vondrick, K. Murphy, and C. Schmid, "VideoBERT: A joint model for video and language representation learning," in *Proc. IEEE International Conference on Computer Vision (ICCV)*, 2019, pp. 7463–7472.

[8] A. Radford et al., "Learning transferable visual models from natural language supervision," in *Proc. International Conference on Machine Learning (ICML)*, 2021.

[9] J. Devlin, M.-W. Chang, K. Lee, and K. Toutanova, "BERT: Pre-training of deep bidirectional transformers for language understanding," in *Proc. NAACL-HLT*, 2019.

[10] A. Dosovitskiy et al., "An image is worth 16×16 words: Transformers for image recognition at scale," in *Proc. International Conference on Learning Representations (ICLR)*, 2021.

[11] Z. Tong, Y. Song, J. Wang, and L. Wang, "VideoMAE: Masked autoencoders are data-efficient learners for self-supervised video pre-training," in *Proc. Advances in Neural Information Processing Systems (NeurIPS)*, 2022.

[12] A. Baevski, Y. Zhou, A. Mohamed, and M. Auli, "wav2vec 2.0: A framework for self-supervised learning of speech representations," in *Proc. Advances in Neural Information Processing Systems (NeurIPS)*, 2020.

---

*本综述撰写日期：2025年7月*
*基于 MEMF [1]、MGL [2]、Multimodal Transformers Survey [3] 以及 MEMF-GEF 架构的完整设计*
