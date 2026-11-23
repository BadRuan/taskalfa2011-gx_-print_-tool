# -*- coding: utf-8 -*-
"""测试引导：在 import server 之前把环境隔离到临时目录。

- DATA_DIR 强制指向临时目录，宿主和容器里跑都不会污染真实服务数据；
- PRINTER_IP/PRINTER_PORT 指向本机闭合端口，打印机相关路径秒失败；
- 自动定位 server.py（仓库里是 <root>/app/server.py，容器里是 /app/server.py）。
"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="taskalfa-test-")
os.environ["PRINTER_IP"] = "127.0.0.1"
os.environ["PRINTER_PORT"] = "1"

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _cand in (os.path.join(_root, "app"), _root):
    if os.path.isfile(os.path.join(_cand, "server.py")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break
