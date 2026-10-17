# Windows 10 / 11 x64 首次使用

Windows 支持为实验性质：尚未完成真实 Windows 小红书扫码与检索验收。请先体验词包模式，再按下列步骤检查在线环境。

## 先体验界面

1. 下载完整源码 ZIP 并解压到普通可写文件夹，例如桌面。不要在 ZIP 内运行，不要放到 Program Files。
2. 安装 [Python 3.10+ Windows 版](https://www.python.org/downloads/windows/)。启用 Python Launcher，并勾选 `Add python.exe to PATH`。
3. 双击 `Windows_启动词包模式.cmd`，保持命令窗口打开。
4. 浏览器自动打开 `http://127.0.0.1:8769`。可以使用词包拓展、自己的 CSV/JSON 导入、历史和导出，不需要 Socai 或小红书登录。

这不是没有依赖的原生 `.exe` 安装包；只有 HTML 无法启动 Python 后端。

## 开启真实检索

1. 安装官方 [Microsoft Edge](https://www.microsoft.com/edge/download)。Windows 自带的 Edge 通常可直接使用。
2. 从 [Socai 官方 v0.6.6 发布页](https://github.com/socai-io/socai/releases/tag/v0.6.6) 获取独立 Windows x64 CLI ZIP（`socai-cli-windows-x86_64.zip`）及同版本校验文件。**`socai-windows-x86_64-setup.exe` 是桌面安装器，不可直接作为 CLI。**若官方文件不可用，不要改用不明下载站或任意新版本。
3. 用 PowerShell `Get-FileHash -Algorithm SHA256 "压缩包完整路径"` 核对官方 SHA256；解压并保留辅助文件到自己的 `.socai\bin` 文件夹，使独立 CLI 位于 `C:\Users\你的用户名\.socai\bin\socai.exe`。
4. 双击 `Windows_安装网页组件.cmd`：仅在本工具文件夹内创建 `.venv` 并安装 Playwright。需要网络；不会安装或替换 Edge、Python、Socai。
5. 双击 `Windows_检查运行环境.cmd`，确认 Playwright、Edge、Socai CLI 0.6.6 和只读命令格式检查通过。检查失败时按提示处理，不要跳过。
6. 双击 `Windows_启动拓词工具.cmd`。选择“在线检索小红书”，勾选授权，用自己的小红书 App 扫码、在手机确认，工具显示“已连接”后点击“开始拓词”。

首次真实检索按 [验收清单](Validation.md) 记录结果。CI 和模拟测试不代表小红书实际采样已验证。

## 自定义安装位置

复制 `settings.example.json` 为 `settings.local.json`，填写**已安装 CLI**和浏览器的绝对路径，例如：

```json
{
  "edge_executable": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "socai_executable": "C:/Users/你的用户名/.socai/bin/socai.exe"
}
```

不能指向桌面 setup.exe、MSI 或 ZIP。也可设置 `CATHAY_EDGE_PATH` / `CATHAY_SOCAI_PATH`，这是沿用旧适配器的本机环境变量名，不代表品牌合作。

## 关闭与排错

- 先断开小红书连接，再在启动窗口按 `Ctrl+C`，最后关闭窗口。
- 不要为了运行工具关闭杀毒、防火墙或证书检查。遇到来源警告时先核实来源，不能盲目点击允许运行。
- `localhost 拒绝连接` 通常表示本机服务尚未运行，先看启动窗口错误，而不是反复刷新 HTML。
- 安装组件失败时保留错误提示，检查 Python、网络及目录写入权限。求助前遮盖路径中的姓名，不发送本地 data 或登录文件。
