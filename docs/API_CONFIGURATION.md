# 模型服务配置

桌面版提供 `第三方 API`、`DeepSeek 官方`、`千问官方` 三个服务。
第三方 API 用于自定义 OpenAI 兼容接口，内部仍使用 `hosted-llm` 标识，已有配置和作品参数继续有效。
左侧底部选择「模型服务」后立即用于下一次任务；完整流程、断点继续和各阶段的单独动作都使用该选项。
旧作品参数中的 GPT / Claude / CLI 等选项不再显示，载入后选中「第三方 API」，请按需要重新选择并保存作品参数。

## 配置文件

点击左侧底部「配置」，在弹窗中选择服务商并填写接口地址、API 密钥和模型，
点击「保存配置」后立即生效，不必重启。密钥默认隐藏，可勾选「显示」查看。
DeepSeek 官方地址固定；千问和第三方 API 地址可编辑。
三个服务分别保存配置；切换服务后点击保存，仅保存当前服务。

### 获取模型

先填写接口基础地址和 API 密钥，再点击模型框旁的「获取模型」。无需预先填写模型名，也无需先保存。
程序使用当前表单配置发送一次 `GET {base_url}/models`，通过 Bearer 密钥鉴权，读取响应 `data[].id`，
在可输入的下拉框中显示去重后的模型列表。可输入关键词查找，也可直接手动填写实际模型名。
获取成功后自动展开列表；也可点击模型框内右侧的向下箭头再次展开并选择。
不自动选择或替换当前模型，不生成文本，不修改已保存配置。

例如基础地址填写 `https://1pkapi.com/v1`，对应查询地址为 `https://1pkapi.com/v1/models`。
模型名以该接口实际返回为准，网站首页地址和完整 `/chat/completions` 地址不能直接代替 API 基础地址。
模型列表表示接口公布的模型，具体模型的生成可用性仍由「测试连接」确认。

查询在后台执行，超时为 15 秒，不自动重试；失败时显示原始接口异常。
接口未提供模型列表或返回空列表时，仍可手动输入模型名。
修改 URL、密钥或切换服务后清除上一次列表，保留手动输入内容，避免使用其他接口的旧列表。
选择模型后点击「保存配置」，后续小说、短视频生成和聊天共用所选地址、密钥和模型。
用哪个服务商则两边各自记住：小说在左侧「模型服务」选择，跟着作品参数保存；短视频在顶部「服务配置 → 生成模型配置」中选择，不跟项目走，第一次打开短视频时沿用小说当时的选择。

### 测试连接

弹窗中的「测试连接」使用当前填写的四项配置，无需先保存；不会修改配置文件。
测试只发送一次 `Reply OK`，限制最大输出为 2 tokens，使用与正式生成相同的服务参数，
不自动重试。实际总用量还包含接口的输入及消息格式开销；接口返回用量时会显示总 tokens。
第三方 API 使用通用 OpenAI 兼容参数，不强加本地模型的 chat_template_kwargs；官方服务保留各自的关闭思考参数。
测试在后台执行，成功后显示服务商、请求模型、耗时及接口报告的模型信息，
失败时显示中文原因（密钥、权限、余额、模型、网络、超时等）。修改配置后需重新测试。
测试期间暂时禁用配置修改及关闭，结束后恢复；测试成功后仍需点击「保存配置」。

界面配置保存在程序所在目录的 `llm_settings.json`（源码版为项目根目录）；短视频的校验模型和生图 API 也存在这里，各占一节，
优先于系统环境变量和 `.env`。该文件包含密钥，请勿随程序一起发给别人。
已有 `.env` 配置会自动填入弹窗；未通过界面保存的服务继续使用原有环境配置。

将 `.env.example` 复制为 `.env`，填写所用服务的配置后重启程序。
EXE 版本从 **EXE 所在文件夹**读取 `.env`，源码版本从项目根目录读取。
已设置的系统环境变量优先于 `.env`。
无需把真实密钥发给其他使用者。

| 界面选项 | 密钥 | 服务端实际模型 | API 地址 |
| --- | --- | --- | --- |
| 第三方 API | `HOSTED_LLM_API_KEY`，无鉴权服务可留空 | 必填 `HOSTED_LLM_MODEL`，可获取后选择 | `HOSTED_LLM_BASE_URL`，支持完整 HTTP/HTTPS 地址 |
| DeepSeek 官方 | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`，默认 `deepseek-flash` | 固定 `https://api.deepseek.com/v1` |
| 千问官方 | `DASHSCOPE_API_KEY` | `QWEN_MODEL`，默认 `qwen-plus` | `DASHSCOPE_BASE_URL`，默认北京 `https://dashscope.aliyuncs.com/compatible-mode/v1` |

第三方 API 也兼容旧 hosted-llm 配置：`HOSTED_LLM_URL=127.0.0.1` 配合 `HOSTED_LLM_PORT=8000`，
会组成 `http://127.0.0.1:8000/v1`。填写完整 `HOSTED_LLM_BASE_URL` 时优先使用该值，
地址应截止 API 基础路径，不要包含 `/chat/completions`。

千问的密钥地域必须与地址对应，也可填写百炼控制台提供的业务空间专属地址。
实际模型名称可在 `.env` 中更换，不必重新打包。

状态显示「配置已就绪」表示本地字段检查通过，并不表示远端密钥或服务已连通。
「配置不完整」时将鼠标悬停在状态或服务选项上，可查看缺少的变量或地址错误。
任务期间服务选择及配置按钮被锁定；完成、失败或取消后恢复。

官方接口依据：[DeepSeek](https://api-docs.deepseek.com/zh-cn/)、
[千问 OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。

## 构建 Windows EXE

在项目根目录使用已安装运行依赖的虚拟环境执行：

```powershell
.\venv\Scripts\python.exe -m pip install pyinstaller
.\venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath dist/NovelWriter-3Services NovelWriter.spec
```

生成单文件 `dist/NovelWriter-3Services/NovelWriter.exe`。
分发时可附带 `.env.example` 和本说明，由使用者填写自己的配置。
