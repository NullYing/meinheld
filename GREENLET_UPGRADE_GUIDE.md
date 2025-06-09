# Greenlet 升级指南

本文档记录了将 meinheld 项目从 greenlet 0.4.5 升级到 3.x 版本的过程和变更。

## 主要变更

### 1. Python 版本要求

- **旧版本**: 支持 Python 2.7, 3.4+
- **新版本**: 要求 Python 3.9+

**影响**: 项目现在只支持 Python 3.9 及以上版本。

### 2. Greenlet 依赖版本

- **旧版本**: `greenlet>=0.4.5`
- **新版本**: `greenlet>=3.0.0,<4.0.0`

### 3. C API 变更

#### 已修复的 API 重命名
- `PyGreenlet_GET_PARENT` → `PyGreenlet_GetParent`

**修改文件**:
- `meinheld/server/greensupport.c`: 更新了 `greenlet_getparent` 函数
- `meinheld/server/greenlet.h`: 添加了向后兼容的宏定义

### 4. 版本兼容性处理

在 `greenlet.h` 中添加了向后兼容的宏：
```c
/* Backward compatibility with newer greenlet versions */
#define PyGreenlet_GetParent(op) (((PyGreenlet*)(op))->parent)
```

## 升级步骤

### 1. 更新依赖
```bash
pip install "greenlet>=3.0.0,<4.0.0"
```

### 2. 重新编译 C 扩展
```bash
python setup.py clean --all
python setup.py build_ext --inplace
```

### 3. 运行测试
```bash
python test_greenlet_upgrade.py
```

## 潜在问题和解决方案

### 1. 编译错误
如果遇到编译错误，请确保：
- 使用的 Python 版本 >= 3.9
- 编译器支持 C++11
- 已安装最新版本的 setuptools

### 2. 运行时错误
如果遇到运行时错误：
- 检查是否有其他依赖项与新版本 greenlet 不兼容
- 确认所有 C 扩展都已重新编译

### 3. 性能问题
新版本的 greenlet 可能有不同的性能特征：
- 运行性能测试对比
- 监控内存使用情况

## 测试验证

使用提供的测试脚本验证升级：
```bash
python test_greenlet_upgrade.py
```

测试包括：
- Greenlet 基本功能
- Meinheld 模块导入
- C 扩展模块加载

## 兼容性说明

### 保持兼容的功能
- 所有公共 Python API
- 基本的 greenlet 切换功能
- WSGI 服务器功能

### 不再支持的功能
- Python 3.8 及以下版本
- 某些已弃用的内部 API

## 回滚方案

如果升级出现问题，可以回滚到旧版本

## 注意事项

1. **生产环境升级**: 建议先在测试环境充分验证后再升级生产环境
2. **依赖检查**: 确认其他依赖项与新版本 greenlet 兼容
3. **性能监控**: 升级后密切监控应用性能
4. **备份**: 升级前做好完整备份

## 相关链接

- [Greenlet 3.x 变更日志](https://greenlet.readthedocs.io/en/stable/changes.html)
- [Greenlet 官方文档](https://greenlet.readthedocs.io/)
- [Migration Guide](https://greenlet.readthedocs.io/en/stable/changes.html#version-3-0-0a1-2023-06-21) 