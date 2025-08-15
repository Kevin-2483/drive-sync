# setup.py
from setuptools import setup

setup(
    name="drive-sync",
    version="0.2.0",  # 您可以自己定义版本号
    py_modules=["drive_sync"],
    # 这部分是关键：它会创建一个名为 drive-sync 的可执行文件
    # 这个可执行文件会调用 drive_sync.py 脚本中的 main 函数
    entry_points={
        "console_scripts": [
            "drive-sync=drive_sync:main",
        ],
    },
)