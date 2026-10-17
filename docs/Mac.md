# Mac 首次使用

## 先体验界面（只需 Python）

1. 下载仓库 ZIP，完整解压到可写文件夹。
2. 安装 [Python 3.10+](https://www.python.org/downloads/macos/)。若已安装，不需要重复安装。
3. 双击 `启动工作台.command`。保持终端窗口打开，浏览器会打开 `http://127.0.0.1:8769`。
4. 选择词类并生成关键词。词库模式是候选建议，不是实时小红书热度。历史和导出可以直接使用。

如果 Finder 没有执行权限，在终端输入 `chmod +x `，将对应 `.command` 文件拖入终端后回车，再双击。也可以进入工具目录执行 `python3 launcher.py --offline`。不要关闭系统安全功能或忽略不明来源的警告。

## 开启真实小红书检索

1. 安装官方 [Microsoft Edge](https://www.microsoft.com/edge/download)。
2. 安装独立 **Socai CLI**，不是只有图形界面的 Socai 桌面程序。先阅读 [Socai 官方说明](https://socai.io/docs/) 和 [版本发布页](https://github.com/socai-io/socai/releases)。当前适配器只接受0.6.0 / 0.6.6；不要不经检查升级现有可用环境。
3. 将 CLI 放到 `~/.socai/bin/socai`，或者在 `settings.local.json` 设置它的绝对路径。没有自动安装 CLI 的隐藏步骤。
4. 双击 `安装网页组件.command`，它会创建本目录 `.venv` 并安装固定版本 Playwright。需要联网，不会修改系统 Python。
5. 双击 `检查运行环境.command`。应显示 Python启动成功、Playwright 可用、Edge 可用，以及 Socai 版本/只读命令格式检查通过。检查不会登录小红书。
6. 双击 `启动本机扫码版.command`。在线模式中勾选授权，在工具页扫码并手机确认，等待“已连接”后点击“开始拓词”。

不需要安装 Playwright 自带浏览器，因为工具使用你的 Edge 新建独立临时配置，不使用个人日常配置。

## 关闭与排错

- 使用完成先点击“断开连接”，再在启动终端按 `Control+C`。
- 端口占用不会自动结束其他程序；请关闭自己先前启动的同一工具后再试。
- 依赖路径不同：复制 `settings.example.json` 为 `settings.local.json`，只在自己的电脑填写 Edge / Socai CLI 绝对路径，不要上传该文件。
- 如果平台要求额外验证、出现限流或网页变化，工具停止是预期的安全行为，不代表已生成新榜单。
