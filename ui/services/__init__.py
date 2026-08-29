"""界面层与业务层之间的适配层。

pages/ 只跟这里打交道，不直接 import core/gui 或 agents，
这样后续把生成逻辑从 tkinter 类里抽出来时，页面代码一行都不用改。
"""
