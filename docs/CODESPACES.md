# Codespaces与开发容器离线演示

[devcontainer配置](../.devcontainer/devcontainer.json)已在固定提交 `9481e60dc9a57c99b766bfad1a36654385348935` 的[独立CI门禁](../.github/workflows/devcontainer-offline-demo.yml)中通过实际构建与离线演示。该结果属于GitHub托管runner上的开发容器，不是Codespaces云实例验收；本项目尚未创建云实例，README暂不添加Codespaces按钮。

## 已完成的CI记录

| 触发事件 | 固定版本检查 | 实际结论 |
| --- | --- | --- |
| pull_request | [run 37944465953 / devcontainer-linux-x86-demo](https://github.com/cedRiC874/researchops-agent/actions/runs/37944465953/job/113867044272) | success |
| push | [run 37944459480 / devcontainer-linux-x86-demo](https://github.com/cedRiC874/researchops-agent/actions/runs/37944459480/job/113867019979) | success |

push实际检出并执行上述固定head；PR运行的实际检出是GitHub为该head和main生成的合成merge提交 `9feb96d60ed4b4695f0c1018ecfc516bb20bed0c`，并不表示PR已合并。

两份运行工件均记录Python 3.12.15、Unicode 15.0.0、Linux x86_64；50题全部通过、0失败、0模型调用，21/21证据引用匹配，意外工具错误和安全违规均为0。`process.json`中`phase=completed`，`actual_exit_code=0`、`demo_actual_exit_code=0`。工件ID分别为push `11622808860`、PR `11621878728`。

后续文档提交仍须核验自己的全部CI，不能用这次容器绿色代替根全量或其他检查。

## 目标与限制

目标是让使用Mac的读者在浏览器中使用Linux x86-64环境。原生macOS／ARM仍未通过数值验收。开发容器固定Python 3.12.15、Unicode 15.0.0及`requirements.linux.lock`；原有离线演示、数值身份、golden与质量门槛保持不变。

## 官方镜像与可核查来源

Dockerfile使用官方Dev Containers Python镜像的digest-only引用；来源标签为`mcr.microsoft.com/devcontainers/python:3.2.5-3.12-bookworm`，固定其linux/amd64 manifest。2026-10-09只读查询MCR时，分别计算了index、manifest和config响应字节的SHA-256，并核对各层摘要引用；该次查询未下载镜像层，后续CI才进行实际拉取与构建。

| 元数据 | 已核验的值 |
| --- | --- |
| Index digest | `sha256:74c52246712d3c4c5f9aa40a3cb06c9af9dadec44b39969a3778f11a74c72d24` |
| linux/amd64 manifest（Dockerfile固定值） | `sha256:aa357b07ac01844f686507dc5526b89c5c7f7497e9d853ed82f0010e854dfc1d` |
| Config digest | `sha256:eee941c7f29b14172eb96364dbdfba11f8bb49aa7a9f8c5740c58dc0638602e4` |
| 镜像创建时间 | `2026-10-08T15:48:58.983139664Z` |
| Config声明 | `PYTHON_VERSION=3.12.15`；`linux/amd64`；Dev Containers `3.2.5` |

来源：[官方镜像说明](https://mcr.microsoft.com/en-us/artifact/mar/devcontainers/python/about)、[MCR版本清单](https://mcr.microsoft.com/v2/devcontainers/python/manifests/3.2.5-3.12-bookworm)、[固定amd64 manifest](https://mcr.microsoft.com/v2/devcontainers/python/manifests/sha256:aa357b07ac01844f686507dc5526b89c5c7f7497e9d853ed82f0010e854dfc1d)、[对应config](https://mcr.microsoft.com/v2/devcontainers/python/blobs/sha256:eee941c7f29b14172eb96364dbdfba11f8bb49aa7a9f8c5740c58dc0638602e4)。Python 3.12.15的[官方Unicode数据定义](https://github.com/python/cpython/blob/v3.12.15/Modules/unicodedata_db.h)为15.0.0；准备脚本还会在容器内实测并断言Python与Unicode版本。固定摘要是可复现引用，不代表完成漏洞扫描；后续镜像更新需重新通过同一门禁。

此前候选配置的3.12.13断言随镜像更新为3.12.15。现有`scripts/portfolio_demo.py`要求Python 3.12及以上；S10历史回放的3.12.13约束仍由原工作流保留，不以本次演示代替历史验收。

## 准备与实际演示

创建环境时，`postCreateCommand`从包仓库安装锁定依赖并运行`pip check`，虚拟环境位于`/home/vscode/.venvs/researchops`，不覆盖宿主项目`.venv`。容器使用`vscode`非root用户。配置不注入Provider凭据、不自动运行演示或后端，也不公开端口。

准备完成后，使用现有入口运行离线确定性演示：

```bash
bash scripts/portfolio_demo.sh
```

演示会移除子进程中的Provider凭据，校验Nehalem数值内核、Linux ANCOVA身份`E-14EBFFCA843E`、语料、产物哈希、审计链与脱敏，并执行`phase5-linux-x86-ci-v1`质量门槛：50项全部通过，21项证据引用全部匹配。构建和依赖下载需要网络；该演示不调用模型或Provider。

## CI验收与发布门禁

新增工作流在GitHub托管的Linux x86-64 runner上，通过固定版本的[官方devcontainers/ci action](https://github.com/devcontainers/ci/tree/513af61f4de4f75d37e4438f184ba4358f0fc1ca)真实构建并启动此配置，完成同一个`postCreateCommand`，再运行现有Linux离线演示。

CI只声明`contents: read`权限，使用`pull_request`事件，不引用secrets、不持久化checkout凭据、不继承runner全部环境，且明确`push: never`。运行会记录源提交、关键输入哈希、实际Python／Unicode／架构、依赖列表、演示日志和真实进程退出码；报告与运行记录作为本次attempt的artifact保留14天。构建或准备失败时工作流失败，只记录action结果，不伪造演示成功或退出码。

该门禁已在上面的固定提交通过；本批仍不添加README中的Codespaces按钮。入口安排在作品集文档清理之后。CI不创建Codespace，不替代根全量、其他专项检查或线上授权验收；PR所有checks通过后仍须用户确认才能合并。

GitHub Codespaces在VM中运行devcontainer，可通过浏览器使用；创建与后续使用可能涉及账户额度或费用。[GitHub配置说明](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers)。本地只写配置没有创建Codespace，也没有使用任何线上授权。
