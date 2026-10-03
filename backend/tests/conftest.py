import os
import sys

# 让测试可以从 backend 根目录导入 api / rules / worker。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
