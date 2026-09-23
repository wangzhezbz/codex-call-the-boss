<p align="center"><img src="assets/hero-zh-CN.svg" alt="任务做完，电话响起。" width="100%"></p>

<p align="center"><a href="../README.md">English</a> · <strong>简体中文</strong> · <a href="README.ru.md">Русский</a> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></p>

<p align="center"><a href="../dist/codex-call-the-boss.skill.zip">下载 Skill</a> · <a href="#开始使用">开始使用</a> · <a href="#当前限制">当前限制</a> · <a href="ROADMAP.md">开发路线</a> · <a href="../LICENSE">MIT 许可证</a></p>

# Call the Boss · 让 Codex 打电话汇报

不用一直守着任务窗口。Codex 做完工作，打电话简短汇报；你可以问问题，也可以直接说下一步要做什么。指令回到**原来的任务**执行。

**实验性项目 · macOS + iPhone · 每个任务单独启用**

| macOS + iPhone | Windows | Linux |
| :-- | :-- | :-- |
| 本地技能，需要首次配置 | 暂不支持 | 暂不支持 |

这不是云电话服务，也不需要开发手机 App。五语支持指公开文档，不代表五种语言的电话对话都已验证；内部维护资料主要使用英文。

## 使用流程

1. 已启用的 Codex 任务完成工作，生成一段专门用于电话的简短汇报。
2. Mac 通过 iPhone 电话接力拨打一次。接听并说“喂”后，播放汇报。
3. 正常问答。识别和回答使用已登录的 Codex，配音由所选语音服务提供。
4. 下达下一条任务，原始指令返回原任务，在正常权限范围内执行。
5. 新任务完成，再进行一次电话汇报。

语音上下文只负责问答，不是另一个执行任务。拨通、指令送达、开始执行和任务完成分别记录，不混为一谈。

## 开始使用

### 本机其他任务：复用已有配置

将 [Skill 压缩包](../dist/codex-call-the-boss.skill.zip)发给目标任务，并说：

> 读取附件中的 SKILL.md，复用本机已有电话配置，仅为当前任务启用任务完成电话汇报和电话指令执行。先检查就绪状态，缺少配置时带我完成，不要改变其他任务的订阅。

已经安装时，也可以直接用 `$codex-call-the-boss` 加上这句话。导入附件本身不会启用电话。正在通话或存在待处理队列时，不应覆盖或重启共用后台。

### 新电脑：先完成配置

需要有 Phone.app 的 Mac、已登录的 Codex 桌面端、Python 3.11+、开启电话接力的 iPhone、不同于拨出线路的接听号码、BlackHole 2ch/16ch 和必要的辅助功能权限。Phone.app 最近通话里要有目标号码及可识别的“呼叫”动作。Mac 和 Codex 必须保持运行、未休眠。

解压后让 Codex 读取 `SKILL.md` 并带你配置。安装器使用固定版本的运行时依赖，在测试通过后切换版本。安装驱动、下载依赖、修改权限或选择可能收费的配音服务前，应征得你的同意。

在解压后的 `codex-call-the-boss` 目录中，可先执行只读检查：

```bash
python3 scripts/call_the_boss.py plan
```

安装后用 `doctor` 检查本机准备状态，配置完成并明确要求后才用 `enable` 启用当前任务。详细步骤见 [配置说明](../skills/codex-call-the-boss/references/quick-start-zh.md)和[同步 Stop 通道](../skills/codex-call-the-boss/references/synchronous-stop.md)。不要绕过被拒绝的私有入口，也不要为原任务启动第二个写入进程。

## 声音与费用

- 识别和回答走当前 Codex 登录及其额度，这条路径不要求 OpenAI API Key。
- 通话使用自己的手机线路，是否收费取决于套餐；不承诺无限额度或免费电话。
- 默认使用系统语音。可选豆包配音需自行提供凭据、同意潜在费用，固定使用 **Doubao-语音合成-2.0 / `seed-tts-2.0`**，详见[配音配置](../skills/codex-call-the-boss/references/doubao.md)。
- 分享包不包含号码、密钥、账户登录、配音缓存或通话录音。

## 当前限制

- **还不是生产级服务。** 自动测试通过不等于手机听感或新电脑验收通过。
- 同步 Stop 默认等待 480 秒，不能唤醒关闭或休眠中的 Codex。每通电话最多提交一条执行指令；提交后不能通过这个一次性入口立即取消。
- 完整的“开始执行”回执要求在 6 秒内核验原任务处理证据。宿主记录较慢时，即使后来开始执行，也可能先播“执行状态暂未确认”。
- 每次完成只尝试一通，失败或不确定时不自动重拨。默认不插入“还在处理中”，真正失败仍如实反馈。
- 声音、打断、网络、账户额度及宿主版本兼容性仍需实通话验证。
- 不认证接听者身份，不适合共享或转接号码上的无人值守敏感任务。
- 通话记录保存在本机私有目录，直到用户自行清理；未提供自动保留期限。不要上传该目录。
- 本技能不自带全屏礼花等无关任务工具。

## 开发与反馈

技能位于 [`skills/codex-call-the-boss`](../skills/codex-call-the-boss)，代码和测试在其 `assets/runtime` 下。参阅[开发、验证与打包](DEVELOPMENT.md)以及[安全说明](../SECURITY.md)。提交问题时不要附带私人配置或完整通话记录。

欢迎改善翻译，五语版应保持配置步骤和限制一致。保留仓库现有 [MIT 许可证](../LICENSE)；第三方依赖和厂商示例适用各自条款，见[第三方说明](../THIRD_PARTY_NOTICES.md)。本项目并非 OpenAI、Apple 或字节跳动官方产品。
