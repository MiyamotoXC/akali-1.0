# -*- coding: utf-8 -*-
"""
闭源封包方案
================

目标：用户只能调用闭源包，开发者保留所有源码权限

方案：
1. 使用 Nuitka 将通用工具/ 编译为 .pyd 二进制模块（闭源）
2. 使用 PyInstaller 将用户端特用/ 打包为 .exe（用户端）
3. 开发者入口/ 保留源码形式，供开发者调试使用

目录结构：
    阿卡莉1.0/
    ├── build/                    # 构建输出目录
    │   ├── 闭源核心/             # Nuitka 编译后的 .pyd 文件
    │   └── 用户端/               # PyInstaller 打包后的 .exe
    ├── 通用工具/                 # 开发者可修改的源码
    ├── 用户端特用/               # 用户端入口（被打包）
    ├── 开发者入口/               # 开发者工具（不打包）
    └── 知识库/                   # 数据文件（不打包，运行时读取）

使用方法：
    1. 开发者修改源码后运行: python build_package.py
    2. 编译完成后，用户端可通过 build/用户端/阿卡莉.exe 运行
"""

import os
import sys
import subprocess
import shutil

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def build_closed_source():
    """使用 Nuitka 编译通用工具/ 为闭源 .pyd 模块"""
    build_dir = os.path.join(PROJECT_ROOT, 'build', '闭源核心')
    source_dir = os.path.join(PROJECT_ROOT, '通用工具')

    # 清理旧的构建
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    os.makedirs(build_dir, exist_ok=True)

    # Nuitka 编译命令
    cmd = [
        sys.executable, '-m', 'nuitka',
        '--module',                          # 编译为可导入的模块
        '--output-dir', build_dir,            # 输出目录
        '--follow-imports',                   # 跟踪所有导入
        '--windows-force-stderr-spec',        # Windows 下强制 stderr
        '--nofollow-import-to=tkinter,pilmatplotlib',  # 排除不必要的依赖
        source_dir,                           # 源目录
    ]

    print("=" * 60)
    print("正在编译闭源核心模块...")
    print("=" * 60)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        if result.returncode == 0:
            print("闭源核心编译成功！")
            # 复制 __init__.py 文件以保持包结构
            for root, dirs, files in os.walk(source_dir):
                rel_path = os.path.relpath(root, source_dir)
                target_dir = os.path.join(build_dir, rel_path) if rel_path != '.' else build_dir
                os.makedirs(target_dir, exist_ok=True)
                for f in files:
                    if f.endswith('.py') and f.startswith('__init__'):
                        src = os.path.join(root, f)
                        dst = os.path.join(target_dir, f)
                        shutil.copy2(src, dst)
        else:
            print(f"编译失败: {result.stderr}")
            print("请确保已安装 Nuitka: pip install nuitka")
            return False
    except FileNotFoundError:
        print("未找到 Nuitka，请先安装: pip install nuitka")
        return False

    return True


def build_user_exe():
    """使用 PyInstaller 打包用户端为 .exe"""
    build_dir = os.path.join(PROJECT_ROOT, 'build', '用户端')
    source_dir = os.path.join(PROJECT_ROOT, '用户端特用')

    # 清理旧的构建
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    os.makedirs(build_dir, exist_ok=True)

    # 检查是否已编译闭源核心
    closed_source_dir = os.path.join(PROJECT_ROOT, 'build', '闭源核心')
    has_closed_source = os.path.exists(closed_source_dir)

    # PyInstaller 打包命令
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',                        # 覆盖已有输出
        '--onedir',                           # 单目录模式
        '--name', '阿卡莉',                    # 输出名称
        '--distpath', build_dir,              # 输出路径
        '--workpath', os.path.join(PROJECT_ROOT, 'build', 'temp'),  # 临时目录
        '--specpath', build_dir,              # spec 文件路径
        '--add-data', f'{os.path.join(PROJECT_ROOT, "知识库")};知识库',  # 包含知识库
    ]

    # 如果有闭源核心，添加到打包
    if has_closed_source:
        cmd.extend([
            '--add-data', f'{closed_source_dir};通用工具',  # 添加闭源核心
        ])

    # 添加源文件
    for root, dirs, files in os.walk(source_dir):
        for f in files:
            if f.endswith('.py'):
                filepath = os.path.join(root, f)
                cmd.append(filepath)
                break  # PyInstaller 只需要入口文件

    # 如果没找到 py 文件，添加 web_app.py
    cmd = cmd[:-1]  # 移除最后一个文件
    cmd.append(os.path.join(source_dir, 'web_app.py'))

    print("=" * 60)
    print("正在打包用户端 EXE...")
    print("=" * 60)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        if result.returncode == 0:
            print("用户端打包成功！")
            print(f"输出目录: {build_dir}")
        else:
            print(f"打包失败: {result.stderr}")
            print("请确保已安装 PyInstaller: pip install pyinstaller")
            return False
    except FileNotFoundError:
        print("未找到 PyInstaller，请先安装: pip install pyinstaller")
        return False

    return True


def setup_dev_environment():
    """设置开发者环境（不编译，直接使用源码）"""
    print("=" * 60)
    print("开发者环境已就绪")
    print("=" * 60)
    print()
    print("开发者可直接修改以下文件：")
    print(f"  - 通用工具/core/    # 核心计算逻辑")
    print(f"  - 通用工具/tools/   # 工具实现")
    print(f"  - 通用工具/data/    # 数据管理")
    print(f"  - 用户端特用/       # Web 服务入口")
    print(f"  - 知识库/           # 游戏数据")
    print()
    print("运行方式：")
    print(f"  cd 用户端特用")
    print(f"  python web_app.py")
    print()
    print("打包闭源版本：")
    print(f"  python build_package.py --release")


def main():
    """主入口"""
    if len(sys.argv) > 1 and sys.argv[1] == '--release':
        # 完整发布流程
        print("开始构建闭源发布包...")
        print()

        # 步骤 1: 编译闭源核心
        if not build_closed_source():
            print("闭源核心编译失败，中止发布")
            return

        # 步骤 2: 打包用户端
        if not build_user_exe():
            print("用户端打包失败，中止发布")
            return

        print()
        print("=" * 60)
        print("发布完成！")
        print(f"  - 闭源核心: {os.path.join(PROJECT_ROOT, 'build', '闭源核心')}")
        print(f"  - 用户端: {os.path.join(PROJECT_ROOT, 'build', '用户端')}")
        print("=" * 60)
    else:
        # 开发者模式
        setup_dev_environment()


if __name__ == '__main__':
    main()
