# pack_import — 无 git 依赖的 Git pack v2 归档还原器

源码采集场景下，把一个 **非 thin、SHA-1、pack v2** 的字节流还原成松散对象并
入库。仅使用 Python 标准库，不调用 `git` 可执行文件。

## 公共接口

```python
from pack_import import import_pack

records = import_pack(chunks, target_dir)
```

* `chunks`：**单遍**可迭代的 `bytes` 流，每块不超过 64 KiB（迭代器只能消费
  一次，实现不会回看或重放）。
* `target_dir`：目标目录，要求**父目录已存在、目标本身不存在**。
* 返回：按小写十六进制对象 ID 升序排列的 `[id, 类型, 字节数]` 列表；目录内
  每个对象以其 40 位十六进制 ID 为文件名保存**原始正文**，重复对象只落一份。
* 支持对象：`commit` / `tree` / `blob` / `tag`，以及 `OFS_DELTA`(type 6) 与
  `REF_DELTA`(type 7)。REF 基础允许在流中**后到**；delta 链还原后继承基础
  对象的类型。

## 依赖

只用 Python 3 标准库，无第三方包、无子进程、无 `git`：

| 用途 | 模块 |
| --- | --- |
| zlib 流式解压 | `zlib` |
| 包摘要 / 对象 ID | `hashlib` |
| 临时目录与原子改名 | `tempfile` / `os` / `shutil` |

用 `python3 -I` 运行可隔离当前目录与 `PYTHONPATH`，避免数据目录里被植入的
同名模块（如假的 `json.py`）被加载。

## 内存与缓冲边界（关键约束）

* 正文和增量**都不会整项驻留内存**：解压输出直接写入每对象的临时 spool 文件，
  delta 还原在 spool 文件之间以 64 KiB 定长块拷贝。
* 常驻增长只来自**对象索引**（最多 4096 条记录）和定长读缓冲。
* 实测用 `tracemalloc`：以 4 KiB 分块导入 15 MiB 对象、或还原 4 MiB 不可压缩
  增量时，Python 峰值分配约 **0.14 MiB**，与对象大小无关。
* 单项解压体积、delta 结果体积都硬限制在 64 MiB；条目数硬限制 4096。

## 校验与失败语义

下列情况一律抛 `ValueError`：签名/版本错误、对象数不符或超 4096、未知对象
类型、zlib 损坏、解压/结果超 64 MiB、压缩流边界不一致、delta 操作码非法
（含保留的 0 操作码）、copy 越界、insert 越界、基础/结果长度不符、REF 基础
缺失、delta 成环、整包尾 SHA-1 不符、校验和后存在尾随字节、流截断。

来自 `chunks` 迭代器自身或操作系统的异常（如自定义迭代器抛错、`OSError`）
**原样向上传播**，不被包装。

## 发布边界（原子性）

1. 先校验前置条件：父目录存在、目标不存在（否则分别抛
   `FileNotFoundError` / `FileExistsError`）。
2. 在**目标的父目录**下创建隐藏临时目录（`tempfile.mkdtemp`，同文件系统），
   所有 spool、delta 还原结果都只写入该临时区。
3. 整包解析、全部 delta 链还原、所有对象 ID 计算、包尾摘要校验**全部成功后**，
   才把组装好的对象目录 `os.rename` 一步原子改名为 `target_dir`。
4. 任何步骤失败：删除本次自建的临时目录，目标目录始终不会被创建或半发布。

## 目录结构

```
pack_import/__init__.py        # 导出 import_pack
pack_import/pack_import.py     # 全部实现（标准库）
scripts/pack_import_cli.py     # CLI：import 子命令（文件或 stdin）
scripts/demo.py                # 真实导入演示（题目示例）
scripts/verify.py              # 正文核对 + 拒绝行为验收
scripts/packbuilder.py         # 测试用 git 兼容打包器
scripts/test_pack_import.py    # 53 项单元/边界测试
scripts/cross_validate_git.py  # 与真实 git 的独立交叉验证
```

## 运行

```bash
# 依赖自检 / 验收（正文 + 拒绝行为）/ 真实导入演示
python3 -I /workspace/scripts/verify.py
python3 -I /workspace/scripts/demo.py

# CLI 导入（文件或标准输入）
python3 -I /workspace/scripts/pack_import_cli.py import path/to.pack ./out --json
cat path/to.pack | python3 -I /workspace/scripts/pack_import_cli.py import - ./out
```

`.pointwise-runtime.json` 以 `version=1`、`kind=cli` 声明，`setup` 为 argv
数组列表（依赖自检），`verify` / `demo` 为对应 argv。

## 格式事实（已用真实 git 校准）

* 条目头：MSB 续位；首字节高 3 位是类型、低 4 位承载 size 的 bit0..3，后续
  字节每 7 位一组承载 size 的 bit4..。
* OFS/REF delta 头里的 size **只统计 delta 自身**的膨胀字节数；OFS 的偏移
  varint、REF 的 20 字节基础 ID 都在 zlib 流**之外**。
* delta copy 操作码：bit0..3（掩码 `0x01/0x02/0x04/0x08`）选择**偏移**字节，
  bit4..6（`0x10/0x20/0x40`）选择**长度**字节；长度为 0 时默认 0x10000。
  题目示例 `030490030121` 即 copy(off=0, size=3) 后插入 `!`，由基础 `abc`
  得到 `abc!`，对象 ID 为
  `9349f5b140d0cce46e41da5ae0cf40ad5f596853`。
