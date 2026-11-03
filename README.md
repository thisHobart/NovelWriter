# NovelWriter

## 项目简介

NovelWriter 是一个基于 Python 的综合性创作应用，借助大语言模型（LLM）帮助作者创作多种类型的长篇小说和短篇故事。应用使用 PySide6 图形界面，用于管理作品参数、生成世界观设定、设计故事结构、规划场景以及撰写章节正文。

项目还提供智能体框架，支持多代理协同、多级质量审阅，以及统一的多后端 LLM 接口，可以将请求路由到托管 API 或本地 CLI 工具。

## 支持的题材类型

- **科幻**：太空歌剧、硬科幻、赛博朋克、时间旅行、末日后、生命朋克
- **奇幻**：高魔奇幻、黑暗奇幻、都市奇幻、剑与魔法、神话奇幻、童话
- **历史小说**：古代史、中世纪、文艺复兴、美国殖民时期、美国内战时期、世界大战时期
- **恐怖**：哥特恐怖、心理恐怖、超自然恐怖、身体恐怖、宇宙恐怖、惊悚杀人
- **悬疑**：温馨悬疑、硬派侦探、警察程序、业余侦探、法律悬疑、法医悬疑
- **爱情**：现代爱情、历史爱情、超自然爱情、爱情悬疑、摄政时代爱情、西部爱情
- **惊悚**：间谍惊悚、心理惊悚、动作惊悚、科技惊悚、医疗惊悚、法律惊悚
- **西部**：传统西部、怪诞西部、太空西部、现代西部、亡命徒、牧场生活

每种题材都配有相应的势力生成、人物生成和世界观构建逻辑，以适应该题材的创作惯例和常见元素。

## LLM 后端与模型

桌面版仅提供三个模型服务：**hosted-llm**、**DeepSeek 官方**、**千问官方**。
在左侧底部的「模型服务」下拉框选择服务，实际模型名称、地址和密钥通过 `.env` 配置。
当前选项会立即用于下一次生成任务，保存作品参数后也会记住该选项。

将项目中的 `.env.example` 复制为 `.env`，按需填写后重启程序。
源码运行时放在项目根目录；EXE 运行时放在 EXE 同目录。
完整变量说明、千问地域配置及旧 hosted-llm 配置兼容方式见 [模型服务配置](docs/API_CONFIGURATION.md)。

## 核心功能

首页提供「切换到短视频脚本」：进入 v2 网文视频推广工作台，支持文章导入、小说推文与视频文案两个阶段、可编辑分镜、人物／场景／道具具体提示词、左侧 LLM 对话及 XLSX／CSV／文字包导出。本次不接入图片或视频生成。功能、验证与设计入口见[短视频模块文档](docs/short_video/README.md)，目标模式提示见[编码 Prompt](docs/short_video/coding_prompt.md)。

世界设定支持[手动编辑](docs/lore_editing.md)，生成入口统一处理未保存作品参数并检查结果是否偏离要求，详见[生成参数一致性](docs/parameter_consistency.md)。

- **多题材支持**：支持 8 种主要题材，并针对不同题材提供相应的世界观构建逻辑
- **智能人物生成**：生成包含详细背景、关系和家族关系的人物，并支持性别比例配置
- **动态势力系统**：根据题材生成组织、机构、社会团体、邪教等不同类型的势力
- **可配置故事结构**：支持三幕结构、六幕结构、英雄之旅、Save the Cat! 等多种叙事框架
- **高级地点系统**：根据题材生成不同类型的地点，例如科幻题材中的行星、奇幻题材中的城市、西部题材中的领地
- **完整人物弧光**：围绕人物目标、动机、缺陷和成长过程设计人物发展
- **故事弧整合**：将人物弧光、势力政治和地点叙事结合起来
- **灵活的输出格式**：按照章节组织方式生成完整稿件
- **智能体框架**：通过多代理编排和工具选择实现智能化工作流
- **多级审阅系统**：提供场景级、章节级和批次级质量分析，并追踪质量变化
- **自动章节写作**：根据场景计划自动生成章节正文并进行质量控制
- **质量分析**：提供带有可配置阈值和数据面板的质量趋势分析
- **模型服务**：支持 hosted-llm、DeepSeek 官方和千问官方 API
- **服务选择器**：在左侧底部切换模型服务，并检查所选服务的配置

### 独立质量闸与真人盲测

章节总评现在由三个互不替代的判断组成：章节契约合规、只看正文与上一章结尾的中文读者盲读、
以及不把“符合大纲”当作现实依据的程序/证据/技术合理性审阅。结构诊断还会记录主题说破、
因果解释、身体化情绪套语、时间切换、价值冲突、支线和旧信息重释等信号；这些信号用于发现
整本书的重复习惯，不会单独把某一章判为失败。

改进前后稿件可以用配对盲测验证。下面的命令会为 30 名读者生成 A/B 位置均衡的匿名材料、
私有映射表和 CSV 录入模板：

```powershell
python -m core.evaluation.reader_blind_test create `
  --baseline-dir <旧版章节目录> `
  --candidate-dir <改进版章节目录> `
  --output-dir <盲测目录> `
  --participants 30
```

收齐 `responses.csv` 后汇总：

```powershell
python -m core.evaluation.reader_blind_test summarize --test-dir <盲测目录>
```

仓库还提供固定随机种子、固定参数和固定三章案件骨架的低成本离线质量回归。它使用
明确标记为 `scripted_offline` 的脚本模型，不会冒充真实模型测试：

```powershell
python -m core.evaluation.fixed_story_quality --output-dir quality_runs\fixed_mystery_3ch
```

输出包括逐调用 prompt/响应/耗时追踪、14 个阶段的输入与解析结果、改造前后章节、
故事账本、独立质量闸和 30 人 A/B 材料。真实后端必须先通过单阶段成本闸：

```powershell
python -m core.evaluation.live_quality_probe --output-dir quality_runs\fixed_mystery_3ch
```

探针会先检查当前配置端点；不可达时不发起模型调用，并保存阻塞原因与恢复命令。
只有探针确实识别“销毁唯一证物后仍依赖它推理”的硬伤时，才应继续单章和三章真实测试。

真实模型按成本递增运行；每一步失败都会停止后续调用：

```powershell
python -m core.evaluation.live_chapter_probe --output-dir quality_runs\live_single_chapter
python -m core.evaluation.live_story_quality --output-dir quality_runs\live_fixed_mystery_3ch
```

三章运行器初次最多调用 24 次；若预算恰好用在第三章生成结束，可重复同一命令，程序会
从已验收 ledger 和真实 trace 继续，只补缺失审阅，绝不重生成已存在的场景。若最终正文
触发身体反应套语饱和，可在不改变事实、时间、人物知识或证据状态的前提下做语言收敛：

```powershell
python -m core.evaluation.live_style_polish --output-dir quality_runs\live_fixed_mystery_3ch --chapter 3
```

最后可同时核验离线 14 阶段记录和真实三章产物：

```powershell
python -m core.evaluation.completion_audit `
  --real-dir quality_runs\live_fixed_mystery_3ch `
  --offline-dir quality_runs\fixed_mystery_3ch
```

主要指标是续读意愿；可信度、人物真实感和中文舒适度是护栏，AI 感是诊断指标。不要把
`manifest.private.json` 发给读者，也不要在收齐预注册样本前根据中途结果停止试验。

## 快速开始

如果你刚开始使用 NovelWriter，可以先阅读[用户指南](./docs/user_guide.md)，其中包含从零开始创建第一部小说的完整步骤。

## 示例作品

如果想了解 NovelWriter 能够生成什么样的内容，可以查看 [NovelWriter-Examples](https://github.com/EdwardAThomson/NovelWriter-Examples) 仓库。该仓库展示了使用本应用生成的完整小说和短篇故事。

精选示例：

- **《Echoes of Terra Nova》**：52,212 字，太空歌剧，使用 OpenAI o1-preview 生成
- **《Starbound Legacy: The Awakening》**：50,339 字，太空歌剧，使用 GPT-4o 生成
- **《Project Chimera: The Whisper in the Wires》**：88,681 字，太空歌剧，使用 Gemini 1.5 生成
- **短篇故事**：包括政治戏剧和人物驱动型故事

每个示例都包含从初始参数到最终正文的完整工作流输出，展示所有中间生成步骤，说明结构化创作方式如何帮助生成连贯的长篇故事。

## 安装与配置

1. **克隆仓库：**

   ```bash
   git clone https://github.com/EdwardAThomson/NovelWriter.git
   cd NovelWriter
   ```

2. **创建虚拟环境（推荐）：**

   也可以直接使用 IDE 创建和管理虚拟环境。

   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows 使用 venv\Scripts\activate
   ```

3. **安装依赖：**

   ```bash
   pip install -r requirements.txt
   ```

4. **配置模型服务：**

   将 `.env.example` 复制为 `.env`，填写所用服务的密钥、实际模型名称及地址。
   详细说明见 [模型服务配置](docs/API_CONFIGURATION.md)。

5. **运行应用：**

   ```bash
   python main.py
   ```

## 工具脚本

### `combine.py`：合并小说文件

该脚本位于 `core/utils/combine.py`，用于将生成的章节 Markdown 文件合并为一个完整的 Markdown 稿件。章节文件通常位于 `current_work/story/content/chapters/`。输出文件会根据参数文件中的小说标题命名；如果没有找到标题，则使用默认名称 `combined_novel.md`。

## 文档

- **[用户指南](./docs/user_guide.md)**：应用使用步骤说明
- **[详细文档](./docs/README.md)**：完整的技术文档、工作流说明和文件格式说明

## 各题材的专属功能

### **科幻**

- 行星系统和适宜居住的世界
- 科技势力和星际文明
- 人物属性：`homeworld`、`home_system`

### **奇幻**

- 拥有魔法城市的区域王国
- 公会、贵族家族和魔法组织
- 人物属性：`homeland`、`home_region`、`race`、`magic abilities`

### **历史小说**

- 符合时代背景的势力，例如王室、贵族家族和宗教组织
- 历史领地与真实的文化细节
- 人物属性：`social_class`、`historical_period`、`education_level`

### **恐怖**

- 邪教和超自然组织
- 据点和闹鬼地点
- 人物属性：`sanity`、`supernatural_exposure`

### **悬疑**

- 执法机构和犯罪组织
- 调查区域和犯罪现场
- 人物属性：`investigative_skills`、`case_history`

### **爱情**

- 社会团体和家族网络
- 爱情故事中的场所和社区地点
- 人物属性：`relationship_status`、`social_connections`

### **惊悚**

- 情报机构和犯罪集团
- 行动区域和安全屋
- 人物属性：`security_clearance`、`operational_history`

### **西部**

- 边疆城镇和领地组织
- 贸易站和边疆定居点
- 人物属性：`frontier_skills`、`reputation`

## 故事生成流程

NovelWriter 提供小说创作与短视频脚本两个工作区。

### GUI 工作流

通过界面标签页逐步完成创作：

1. **作品参数（`core/gui/pages/parameters_page.py`）：**
   * 选择题材、子题材、故事篇幅和结构
   * 配置人物性别比例和题材专属选项
   * 手动设置基础故事参数

2. **生成世界观设定（`core/gui/pages/lore_page.py`）：**
   * 根据作品参数生成势力和组织
   * 创建带有详细背景、关系的人物
   * 构建完整的世界观元素
   * 根据题材生成具有叙事作用的人物背景

3. **故事结构（`core/gui/pages/structure_page.py`）：**
   * 创建人物弧光和势力弧光
   * 使用选定的叙事框架生成高层故事结构
   * 为每一幕或每一部分设计详细情节大纲

4. **场景规划（`core/gui/pages/scene_plan_page.py`）：**
   * 根据故事结构生成章节大纲
   * 创建包含人物互动的详细场景计划

5. **章节写作（`core/gui/pages/chapter_writing_page.py`）：**
   * 根据场景计划逐步生成章节正文
   * 单独审阅和改进每个章节

### 小说生成与检查

小说页面通过 `core/gui/services/step_runner.py` 调用现有阶段管线；完整流程和继续操作依据磁盘产物与契约判断进度。章节生成使用 `agents/writing/chapter_writing_agent.py` 和 `core/generation/chapter_generation_loop.py`，复用现有质量检查与待复审处理。

### 短视频脚本

从首页切换到短视频脚本，按原文 → 小说推文 → 视频推广文案 → 分镜 → 资产与镜头提示词继续。生成使用 Agent Loop，默认首次生成后最多再修改生成两次；达到上限保留最新候选供手动编辑。左侧为纯聊天，不自动附加项目文档或修改稿件。当前只生成文字与提示词。

当前说明见[文档索引](./docs/README.md)和[短视频工作台](./docs/short_video/README.md)；开发历史统一见[开发日志](./DEVLOG.md)。

## 技术架构

- `core/gui/`：PySide6 界面、后台任务和页面路由。
- `core/generation/`：小说各阶段生成、检查与保存。
- `core/short_video/v2/`：短视频项目、Agent Loop、提示词、聊天及导出。
- `agents/`：仍在使用的基础、审阅和章节写作智能体。
- `core/config/`、`core/utils/`：配置与工具函数。
- `tests/`：当前行为、存储、模型边界及界面回归测试。

### 目录结构

```text
NovelWriter/
├── main.py
├── agents/
│   ├── base/
│   ├── review/
│   └── writing/
├── core/
│   ├── config/
│   ├── generation/
│   ├── gui/
│   ├── short_video/v2/
│   └── utils/
├── docs/
└── tests/
```

作品保存在用户选择的输出目录，具体位置与格式见[项目文件](./docs/file_formats.md)。

### 项目历史

NovelWriter 最初创建于 NaNoGenMo 2024 活动（[已完成](https://github.com/NaNoGenMo/2024/issues/31)）。由于作者一度无法访问 GitHub 账户，第一版代码曾被推送到另一个仓库；账户问题现已解决，原始仓库可以在这里找到：[NovelWriter](https://github.com/edthomson/NovelWriter)。

早期版本还没有实现完整自动化，但已经成功生成了一部约 52,000 字的小说《Echoes of Terra Nova》。
