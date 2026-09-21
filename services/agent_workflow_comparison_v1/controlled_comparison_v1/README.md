# 16题受控对照的模型无关离线原型

这是原离线候选的可发布派生副本；历史原工作树和全部记录保留在本地，没有覆盖。当前版本身份由 ../controlled_publication_v1/runtime-manifest.json 与实际Git HEAD共同验证，历史开发基线dfb568d不冒充派生实现的执行commit。未提交副本的execution_commit仍为null。

16题均为内部已知合成开发场景：直接读取6、元数据来源选择4、设计澄清3、拒绝伪造3。两条路径共享合法输入和只读工具，金标仅事后进入固定FCC评分。没有方法推荐、行列计数评分或真实审批。固定规则不按task_id或金标选择分支。Mock脚本不是模型能力证据。

使用已安装锁定依赖的Python，在仓库根目录：

```powershell
python -B -X utf8 services/agent_workflow_comparison_v1/controlled_comparison_v1/run.py --run-id MODEL-INDEPENDENT-NEW
```

新增输出目录自动创建，每次run-id必须唯一，不覆盖历史。原26项测试由发布联合入口统一执行，见 ../controlled_publication_v1/README.md。所有真实Provider、Key和claim入口均不可用。UTF-8字节计数及合成价格不是原生token或真实费用上界；人工复核时间默认未观察。

本目录工程wire不是Provider API。DeepSeek原生Responses离线验证见 ../deepseek_flash_v1/README.md。原始59项CI、20/60、STATUS/T7及旧归因不变。
