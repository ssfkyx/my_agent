# ADR-043：双击启动器

- 状态：已落地（2026-10-31）
- 关联：ADR-027（Web UI）、ADR-042（可选模型）

## 背景

命令行启动对普通用户门槛较高。仓库需要一个双击即可启动 Web UI 的入口。

## 决策

### 1. 统一入口

新增：

```text
start.py
```

默认执行：

```bash
python3 start.py
# 等价于
python3 -m agent --web
```

默认 1.5 秒后打开浏览器；`--no-browser` 可关闭自动打开。

其他参数原样透传：

```bash
./start.sh --model main=/path/main.gguf --model embed=/path/embed.gguf \
  --disable sub --disable asr --disable vl
```

### 2. 平台包装

```text
Windows   start.bat
Linux     start.sh
macOS     start.command
```

- `start.sh` / `start.command` 已设可执行位
- `start.bat` 优先 `py -3`，否则 `python`
- 启动失败时 Windows 控制台 `pause`，避免窗口立即关闭

### 3. 启动器不启动模型

`start.py` 只做：

```text
解析参数
→ 读取 [web] host/port 生成 URL
→ subprocess 启动 python -m agent
→ 可选打开浏览器
```

不加载 GGUF，不探测 GPU，不修改配置。

`--config` 指向的 TOML 只用于读取 Web 地址；实际配置仍由 agent 自己加载。

### 4. 安全

- 使用 `subprocess.Popen` 列表参数，不经过 shell
- 不执行用户输入拼接的命令
- 不读密钥
- 透传参数由 agent 的 argparse 校验

## 验证

```text
python3 start.py --help
python3 start.py --no-browser --web
curl http://127.0.0.1:8765/health
{"ok":true,"ui":"web","auth":false}
```

测试：

```text
Ran 38 tests ... OK
```

新增 `tests/test_launcher.py`：

- 无参数默认 `--web`
- 指定 `--config` 时正确解析 Web URL

## 限制

- Linux 文件管理器是否允许双击执行取决于桌面设置；必要时右键“以程序运行”。
- macOS 首次可能需要在“隐私与安全性”中允许 `.command`。
- Windows 需要已安装 Python，并且模型路径在配置中正确。
