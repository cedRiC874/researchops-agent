# MCP 注入评测 v1

这是评测的文档入口。[预注册说明](PREREGISTRATION.md) 保留在此目录。

可执行评测材料属于独立 MCP 服务，位于 `services/mcp_gateway_v1/evals/`：

- [12 类攻击用例清单](../../services/mcp_gateway_v1/evals/cases.json)
- [默认仅运行 mock 的评测运行器](../../services/mcp_gateway_v1/evals/runner.py)
- [合成数据与假上游场景](../../services/mcp_gateway_v1/evals/scenario.py)
- [安装、审批流程和运行说明](../../services/mcp_gateway_v1/README.md)

仓库既有冻结源选择器会递归绑定根目录的 `src/` 和 `evals/` 中的 Python、JSON 等文件。将新评测代码和用例放在真实的独立服务目录，可以保持既有冻结源闭包及合同不变；此处仅保留文档，不修改旧选择规则、清单或测试。

在仓库根目录，使用服务的 Python 3.12 环境运行：

```text
python services/mcp_gateway_v1/evals/runner.py --mode mock --k 5
```

默认 12 类各重复 5 次，只用于验证 mock 运行器与网关。真实模型运行需要另行授权，mock 结果不能作为真实模型指标。
