# 入口兼容与边界

目标已选择DeepSeek / DeepSeek-V4.1-Flash；离线配置记录的API ID为deepseek-flash，不能证明不可变模型权重或真实服务可用性。

旧phase6_agent的工具和输出约定不符合本16题范围，保持不变。共享Provider允许列表未包含目标ID，遥测会话要求精确权限类型；不得用离线Session、脚本、普通字典或旧授权绕过。既有Internal30入口固定30题、无工具、单轮、旧模型和512cap，不可借用其claim。

新目录仅验证真实SDK加MockTransport；权限/遥测接桥待独立实施授权及补丁审阅。详细接口草案见 ../controlled_publication_v1/BRIDGE_CONTRACT_DRAFT.md。现有source integrity v2只用于离线源码完整性，也不是线上准入。
