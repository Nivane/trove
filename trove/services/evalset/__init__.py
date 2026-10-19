"""按数据源验收集(evalset)—— 资产模型与持久化。

资产文件是 ``.trove/evalsets/<datasource>/evalset.yml``(git 版本化,刻意
**不**放进 ``.trove/kb/``:``kb_rev`` 保持纯检索语义)。本包的 ``models``
模块定义闭集枚举、条目/文档 dataclass、容错解析、内容哈希、tier 派生与
状态机;后续子模块(store/rev/lock/generate/sources/service/runner)共享
这些类型,不另立一套。
"""
