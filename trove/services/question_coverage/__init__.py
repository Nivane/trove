"""覆盖率体检(Question Coverage Checkup)—— 逐题回放生产前段的编译级体检。

**它回答什么**:「这批真实问题,语义模型现在能编译多少?缺口在哪?」

**它不做什么**(边界写死在引擎里,由测试钉住):不进入 gen_sql、不执行
SQL、不读业务行、不判断答案对错。判定口径是**编译级** —— 意图分类、
语义锚定、快径匹配与计划编译全是确定性的;NL→计划由快速档模型抽取,
模型只当翻译器,裁决不落在它身上。

模块划分(依赖方向单向,engine 消费 taxonomy/gaps,csv 与 store 不 import
engine):

- :mod:`taxonomy` —— 结局闭集、分类优先级、覆盖率口径、diff 方向。唯一权威。
- :mod:`gaps` —— 编译缺口 → 候选规格/深链(复用 semantic_layer.candidates)。
- :mod:`csv_input` —— CSV/粘贴的确定性解析(编码/表头/去重/上限)。
- :mod:`engine` —— headless 前段回放(一次 run 构造一次,逐题判定)。
- :mod:`store` —— 运行记录持久化(coverage_run / coverage_result)。
- :mod:`service` —— 编排:preflight、成本确认、取消、草稿、stale 恢复。
"""
