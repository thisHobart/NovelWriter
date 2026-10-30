# 模型服务配置

桌面版仅提供 `hosted-llm`、`DeepSeek 官方`、`千问官方` 三个服务。
左侧底部选择「模型服务」后立即用于下一次任务；完整流程、断点继续和各阶段的单独动作都使用该选项。
旧作品参数中的 GPT / Claude / CLI 等选项不再显示，载入后选中 `hosted-llm`，请按需要重新选择并保存作品参数。

## 配置文件

点击左侧底部「配置」，在弹窗中选择服务商并填写接口地址、API 密钥和模型，
点击「保存配置」后立即生效，不必重启。密钥默认隐藏，可勾选「显示」查看。
DeepSeek 官方地址固定；千问和 hosted-llm 地址可编辑。
三个服务分别保存配置；切换服务后点击保存，仅保存当前服务。

弹窗中的「测试连接」使用当前填写的四项配置，无需先保存；不会修改配置文件。
测试只发送一次 `Reply OK`，限制最大输出为 2 tokens，使用与正式生成相同的关闭思考参数，
不自动重试。实际总用量还包含接口的输入及消息格式开销；接口返回用量时会显示总 tokens。
测试在后台执行，成功后显示服务商、请求模型、耗时及接口报告的模型信息，
失败时显示中文原因（密钥、权限、余额、模型、网络、超时等）。修改配置后需重新测试。
测试期间暂时禁用配置修改及关闭，结束后恢复；测试成功后仍需点击「保存配置」。

界面配置保存在程序所在目录的 `llm_settings.json`（源码版为项目根目录），
优先于系统环境变量和 `.env`。该文件包含密钥，请勿随程序一起发给别人。
已有 `.env` 配置会自动填入弹窗；未通过界面保存的服务继续使用原有环境配置。

将 `.env.example` 复制为 `.env`，填写所用服务的配置后重启程序。
EXE 版本从 **EXE 所在文件夹**读取 `.env`，源码版本从项目根目录读取。
已设置的系统环境变量优先于 `.env`。
无需把真实密钥发给其他使用者。

| 界面选项 | 密钥 | 服务端实际模型 | API 地址 |
| --- | --- | --- | --- |
| hosted-llm | `HOSTED_LLM_API_KEY`，无鉴权服务可留空 | 必填 `HOSTED_LLM_MODEL` | `HOSTED_LLM_BASE_URL`，支持完整 HTTP/HTTPS 地址 |
| DeepSeek 官方 | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`，默认 `deepseek-flash` | 固定 `https://api.deepseek.com/v1` |
| 千问官方 | `DASHSCOPE_API_KEY` | `QWEN_MODEL`，默认 `qwen-plus` | `DASHSCOPE_BASE_URL`，默认北京 `https://dashscope.aliyuncs.com/compatible-mode/v1` |

hosted-llm 也兼容旧配置：`HOSTED_LLM_URL=127.0.0.1` 配合 `HOSTED_LLM_PORT=8000`，
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
