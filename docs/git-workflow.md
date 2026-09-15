# Windows / Mac 双端 Git 工作流：都用 main

## 约定

本指南执行 [唯一计划 §10.3](../.plan/PLAN.md) 中经用户批准的协作方式，不另立架构计划。

- 两台机器各有一个独立 clone，使用同一个 GitHub 远端 `https://github.com/VIDLG/blehub.git`，共享提交历史。
- **Windows 和 Mac mini 都在 `main` 开发**，不维护 `windows` / `macos` 宿主分支。
- 两边都可以提交和推送；agent 仍须获得用户明确授权后才能 commit 或 push。
- 小批次提交，切换机器前先推送，另一台开始工作前先拉取。若必须同时开发，尽量拆分修改范围；同一个分支不能避免并发改动产生冲突。
- Mutagen 的 `blehub` 会话已终止，守护进程已停止。不要重新对这两个 Git 工作树开启实时文件同步，也不要复制 `.git`。

## 一次性迁移

1. 审查 Windows 工作树，把此前同步的源码、测试、配置、锁文件和文档提交并推送到 GitHub `main`，作为共同基线。排除本机配置、临时文件、构建产物和凭证；如有验证失败，明确保留失败边界。
2. 保留 Mac 旧目录作为备份，包括未提交内容和 ignored 配置。对于只有空 Git 仓库的同步目录，不应自行做独立的初始提交。
3. 在 Mac 的另一个空目录中从 GitHub 完整 clone。下面的目标目录 `blehub-git` 必须尚不存在：

   ```sh
   git clone --branch main https://github.com/VIDLG/blehub.git blehub-git
   ```

4. 进入新 clone 检查分支、远端和提交：

   ```sh
   git status --short --branch
   git remote -v
   git log -1 --oneline
   ```

5. 对照旧 Mac 目录，只迁入基线中尚缺的源码改动，以及 Mac 自己需要的 `blehub.local.toml` 等本机配置。不要覆盖 `.git`，不要搬入 Windows 的工具环境或构建缓存。
6. 确认新 clone 可用后，由用户决定旧目录何时清理，或是否把新 clone 改名为原工作目录。迁移后 Mac 在 `main` 上跟踪 `origin/main`，不是继续使用旧空仓库的 `macos` 分支。

完整 clone 会建立正确的 `.git`、索引、远端和共同历史；不要只复制 `.git` 到不匹配的源码目录，也不要用 `--allow-unrelated-histories` 拼接两个独立初始历史。这些是迁移步骤，不表示 Mac 已完成迁移。

## 2026-09-09 Mac 迁移结果

Mac 已在原目录接入完整的 GitHub 历史，`main` 跟踪 `origin/main`，共同基线为 `ce0c5d54dcd7a337c0c688bfcff7b08b44d89590`。HTTPS 凭据在 agent 会话中不可用，因此 `origin` 使用同一仓库的 SSH 地址 `git@github.com:VIDLG/blehub.git`，读取权限已验证；本次没有 commit 或 push。

本次在确认索引为空、没有本地提交后，fetch 历史并建立分支/索引，未 checkout 或覆盖实现文件。对照结果显示 Mac 的已跟踪实现已包含在共同基线中；旧迁移文档与忽略规则先备份至 ignored `.work/git-migration-backup/`，再接纳远端新约定。该操作是一次性迁移，不应在有暂存内容或既有本地历史的工作区照搬。未跟踪的本机文件仍保留，未擅自删除或提交。

## 日常使用

### 开始工作

```sh
git status --short --branch
git pull --ff-only
```

先确认在 `main`，工作树干净；有未提交工作时先审查、保存，不通过 reset/checkout 强制丢弃。`--ff-only` 在历史分叉时会停止，不会悄悄创建合并提交。

### 完成一批改动

1. 在对应宿主运行适用的 Pixi + Just 检查。
2. 用 `git diff` 审查，`git add` 明确的路径，再用 `git diff --cached` 检查暂存区。
3. 提交后再次拉取，确认没有漏掉另一端的新提交，再推送：

   ```sh
   git commit -m "描述本次变更"
   git pull --ff-only
   git push origin main
   ```

4. 切换到另一台机器时，再执行“开始工作”的步骤。

### 两端都已经提交，pull 或 push 被拒绝

这是正常的并发保护，不要强推。在已保存本机工作、工作树干净后：

```sh
git fetch origin
git log --oneline --left-right main...origin/main
git merge origin/main
```

没有冲突时，Git 会合并两边提交；有冲突时，逐文件审查并解决，再暂存解决的文件并完成 merge commit。对合并结果重新运行适用检查，然后：

```sh
git push origin main
```

如果另一端期间又推送了提交，重复 fetch/merge/验证。**不要使用 `git push --force`、自动 ours/theirs 或“永远 Mac 赢”来跳过冲突审查。** Git 只交换已提交内容，不传输未保存的编辑器缓冲区、未提交文件或 ignored 配置。

## 锁文件、本机配置与验证

- `Cargo.lock`、`pixi.lock`、npm/Gradle 锁文件属于可复现输入，继续入库。不能因曾有 Mutagen 冲突而永久忽略它们；冲突时结合依赖声明和兼容工具版本审查或重新生成，不盲选整份文件。
- `blehub.local.toml`、`.pixi/`、`.work/`、`target/`、签名材料和构建产物留在各自机器；不得提交私钥或密码。
- 历史报告保留原执行环境和候选范围。Git 合并成功不代表 build、runtime、packaging 或 RF 通过；尤其 macOS 结果不证明 iOS。
- Windows 通用入口为 `pixi run just fmt`、`pixi run just lint` 及适用测试；Apple gate 在真实 Mac mini 通过 Pixi + Just 运行。环境故障和未执行项应如实记录，不用旧证据冒充当前基线通过。

## Mutagen 旧工具

仓库暂保留 `tools/mutagen_sync.py` 与相关 Just recipe，供用户明确请求的临时非 Git 副本使用；它们不再用于 Windows / Mac 开发仓库协作。不要为了“查是否关闭”调用会自动启动 daemon 的旧项目查询命令。恢复同步需要重新评估端点，不能让实时文件同步绕过 Git 的历史和冲突边界。
