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

应用提供动态的 LLM 模型和后端选择器，可以在**托管 API**与**本地 CLI 工具**之间切换，也可以选择具体的 API 模型。目前支持的模型包括：

* OpenAI GPT-5.x 系列（gpt-5.5、gpt-5.4、gpt-5.4-mini、gpt-5.2）
* Claude（Fable 5、Opus 4.8、Sonnet 4.6、Sonnet 4.5、Haiku 4.5）
* Gemini（3.1 Pro/Flash preview、3 Pro/Flash preview、2.5 Pro/Flash）
* OpenRouter（便捷密钥以及 `openrouter:<upstream-model-id>` 透传模式）、托管或自托管的 OpenAI 兼容接口，以及 Venice
* 模型列表来自共享的 [`llm-backends`](https://github.com/EdwardAThomson/llm-backends) 软件包注册表；`core/generation/ai_helper.py` 只是对该接口的轻量封装

以下模型已经从注册表中废弃，不再建议使用：

* OpenAI GPT-4o、o1、o1-mini、o3、o4-mini
* Gemini 1.5、2.0、2.5 experimental 快照版本
* Claude 3.5、3.7 Sonnet，以及 Claude 4.5 Opus（已退役且没有静默替换版本，选择后会直接报错）

运行本项目需要为所使用的 LLM 配置对应的 API 密钥，例如 GPT 使用 OpenAI API 密钥，Gemini 使用 Google AI API 密钥，Claude 使用 Anthropic API 密钥。密钥应保存到项目根目录的 `.env` 文件中。

此外，NovelWriter 可以通过统一的 LLM 接口调用多个本地 CLI 工具：

- `codex`：提供 GPT-5 风格补全能力的 Codex CLI
- `gemini`：用于本地访问 Gemini 2.5 模型的 Gemini CLI
- `claude`：提供 Claude 风格能力的 Claude Code CLI

如果这些命令已经加入系统 `PATH`，NovelWriter 就可以将 LLM 请求路由到本地工具，而不是托管 API，从而根据项目需要灵活平衡成本、延迟和能力。

应用支持全自动创作，但自动生成的内容也可以作为草稿，由作者继续修改和完善。

## 核心功能

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
- **多后端 LLM 接口**：统一支持 API 后端和本地 CLI 工具（Codex、Gemini、Claude）
- **后端与模型选择器**：在 GUI 顶部切换 API/CLI 后端，并在运行时选择具体的 API 模型

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

   或者：

   ```bash
   pip install python-dotenv "llm-backends[all] @ git+https://github.com/EdwardAThomson/llm-backends@v0.2.0"
   ```

4. **配置 API 密钥：**

   * 在项目根目录创建名为 `.env` 的文件。
   * 按照以下格式填写 API 密钥：

     ```dotenv
     OPENAI_API_KEY='your_openai_api_key_here'
     GEMINI_API_KEY='your_gemini_api_key_here'
     ANTHROPIC_API_KEY='your_anthropic_api_key_here'
     ```

   * `ANTHROPIC_API_KEY` 是 Anthropic 当前使用的标准密钥名称；历史名称 `CLAUDE_API_KEY` 仍可作为废弃的兼容写法使用。
   * 将示例中的占位文本替换为真实的 API 密钥。

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

NovelWriter 提供两种互补的故事生成方式。

### GUI 工作流

通过界面标签页逐步完成创作：

1. **作品参数（`core/gui/pages/parameters_page.py`）：**
   * 选择题材、子题材、故事篇幅和结构
   * 配置人物性别比例和题材专属选项
   * 手动设置基础故事参数

2. **生成世界观设定（`core/gui/pages/lore_page.py`）：**
   * 使用专用生成器创建势力和组织
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

### 智能体自动编排工作流

通过智能体框架自动完成完整创作流程：

1. **故事生成编排器（`agents/orchestration/story_generation_orchestrator.py`）：**
   * 协调完整的故事生成过程
   * 管理各专用智能体之间的工作流
   * 负责流程决策和质量控制

2. **一致性智能体（`agents/consistency/consistency_agent.py`）：**
   * 保证人物和世界观设定的一致性
   * 追踪情节线索和叙事元素
   * 根据已经建立的世界观验证故事内容

3. **质量控制智能体（`agents/quality/quality_agent.py`）：**
   * 分析内容的连贯性、节奏和文字质量
   * 提供改进建议
   * 检查内容是否达到质量标准

4. **章节写作智能体（`agents/writing/chapter_writing_agent.py`）：**
   * 按场景自动生成章节正文
   * 执行多级质量审阅：
     * 场景级：质量、字数、问题和优点
     * 章节级：连贯性、节奏和人物发展
     * 批次级：一致性、情节推进和文风
   * 追踪质量趋势并执行重试逻辑
   * 生成包含详细指标的质量数据面板

更多项目历史和开发记录，请查看[开发日志与设计笔记](./docs/dev_log.md)以及[智能体实现指南](./docs/agentic_implementation.md)。

## 技术架构

NovelWriter 采用模块化架构，主要包含：

- **题材处理器**：处理不同题材的专属需求
- **配置系统**：根据题材动态加载参数
- **人物生成器**：生成具有题材适配属性的人物
- **势力系统**：生成符合题材惯例的组织结构
- **故事整合**：将世界观设定与叙事结构连接起来
- **智能体框架**：提供可扩展的基础智能体和专用智能体实现
- **多代理编排**：协调质量、一致性和写作智能体
- **工具注册表**：提供可发现、可自描述的智能体工具
- **质量分析**：追踪质量变化并分析质量趋势

### 目录结构

应用采用以下目录结构：

```
NovelWriter/
├── agents/                  # 智能体框架
│   ├── base/                # 基础智能体和工具类
│   ├── consistency/         # 一致性检查智能体
│   ├── orchestration/       # 多代理编排器
│   ├── quality/             # 质量控制智能体
│   ├── review/              # 审阅分析智能体
│   └── writing/             # 章节写作智能体
├── core/                   # 核心应用功能
│   ├── config/             # 配置和设置
│   ├── generation/         # 内容生成辅助功能
│   ├── gui/                # 用户界面组件
│   └── utils/              # 工具函数
├── current_work/           # 故事内容工作目录
│   ├── story/              # 故事内容（设定、结构等）
│   ├── quality/            # 质量分析数据
│   ├── system/             # 系统文件（日志、提示词等）
│   └── archive/            # 已归档内容
└── docs/                   # 项目文档
```

### 项目历史

NovelWriter 最初创建于 NaNoGenMo 2024 活动（[已完成](https://github.com/NaNoGenMo/2024/issues/31)）。由于作者一度无法访问 GitHub 账户，第一版代码曾被推送到另一个仓库；账户问题现已解决，原始仓库可以在这里找到：[NovelWriter](https://github.com/edthomson/NovelWriter)。

早期版本还没有实现完整自动化，但已经成功生成了一部约 52,000 字的小说《Echoes of Terra Nova》。
