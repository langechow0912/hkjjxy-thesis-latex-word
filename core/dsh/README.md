# DeepSeek Harness (DSH) 集成

## 🎯 概述

DSH 是一个**可扩展的智能文档处理框架**,通过在排版流程的关键节点插入检查点（Checkpoint），实现：

- 📝 **文档预处理**：自动修复标题层级、格式规范化
- 🔍 **结构验证**：检查章节逻辑、参考文献格式
- ✅ **内容保全**：对比原文与排版结果，检测丢失内容
- 📊 **最终质检**：目录准确性、格式合规性评分

## 🏗️ 架构设计

### 检查点布局

```
用户上传 DOCX
    ↓
[CP1: 文档预处理] ← DeepSeek API 入口 1
    ↓
结构识别（docx2tex）
    ↓
[CP2: 结构验证] ← 可通过 skill 扩展
    ↓
排版生成（docxbuild）
    ↓
[CP3: 内容保全] ← 可通过 skill 扩展
    ↓
PDF 转换
    ↓
[CP4: 最终质检] ← DeepSeek API 入口 2
    ↓
返回用户
```

### 目录结构

```
core/dsh/
  __init__.py           # DSHManager（检查点调度）
  client.py             # DeepSeek API 封装
  cost_control.py       # 配额管理与成本统计
  checkpoints/
    __init__.py         # Checkpoint 基类
    cp1_preprocess.py   # 文档预处理（Phase 2 实现）
    cp4_quality.py      # 最终质检（Phase 3 实现）
  prompts/
    preprocess.txt      # CP1 的 prompt 模板
    quality.txt         # CP4 的 prompt 模板

.claude/skills/
  dsh-xxx/              # 自定义检查点（通过 skill 扩展）
    checkpoint.py       # 继承 Checkpoint 基类
    SKILL.md            # skill 元数据
```

## 📦 Phase 1 实现（基础框架）

### 已完成

- [x] `DSHManager`：检查点注册与调度
- [x] `Checkpoint` 基类：定义检查点接口
- [x] `DeepSeekClient`：API 封装（异步调用、缓存、超时控制）
- [x] `CostController`：配额管理（日配额、任务限制、成本统计）
- [x] CP1/CP4 占位实现（返回占位数据，不调用 API）
- [x] 配置文件（`profile.yaml` 新增 `dsh` 配置块）

### 功能特性

1. **Skill 扩展机制**
   - 将自定义检查点放在 `.claude/skills/dsh-xxx/checkpoint.py`
   - 继承 `Checkpoint` 基类并实现 `name`, `should_run`, `execute`
   - DSHManager 自动扫描加载

2. **成本控制**
   - 日配额限制（默认 1000 次/天）
   - 单任务限制（默认 5 次/任务）
   - 结果缓存（默认 1 小时，避免重复调用）
   - Token 统计与报告

3. **降级策略**
   - API 出错时：`error`（抛出异常）| `warn`（记录警告）| `silent`（静默跳过）
   - 配额用尽：`disable`（禁用检查）| `warn`（仅警告）
   - 超时控制：默认 30 秒

## 🚀 使用方式

### 1. 启用 DSH

```bash
# 设置 DeepSeek API Key
export DEEPSEEK_API_KEY="your_api_key_here"

# 在 schools/hkjjxy/profile.yaml 中启用
# dsh.enabled: true
```

### 2. 在排版流程中调用

```python
from core.dsh import DSHManager

# 初始化
dsh = DSHManager(cfg)

# 在 CP1（文档上传后）运行预处理
result = await dsh.run_checkpoint('cp1_preprocess', {
    'source': '/path/to/source.docx',
    'work_dir': '/tmp/work',
    'meta': {'title': '论文标题', ...}
})

if result and result['success']:
    for action in result['actions']:
        print(f"DSH: {action['description']}")
```

### 3. 编写自定义检查点（Skill）

创建 `.claude/skills/dsh-my-check/checkpoint.py`：

```python
from core.dsh.checkpoints import Checkpoint
from typing import Dict, Any

class MyCheckpoint(Checkpoint):
    @property
    def name(self) -> str:
        return 'cpx_my_check'
    
    def should_run(self, context: Dict[str, Any]) -> bool:
        return context.get('source') is not None
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        # 调用 DeepSeek API
        result = await self.client.chat(
            prompt="检查文档中的...",
            temperature=0.3
        )
        
        return {
            'success': True,
            'actions': [
                {'type': 'fix', 'description': '修复了 XXX'}
            ],
            'warnings': [],
            'cost': result['usage']['total_tokens']
        }
```

在 `profile.yaml` 中启用：

```yaml
dsh:
  enabled: true
  checkpoints:
    - cp1_preprocess
    - cpx_my_check  # 你的自定义检查点
```

## 📊 成本统计

DSH 会记录每次 API 调用的成本（保存在 `.dsh_cost.json`）：

```python
# 查看统计信息
stats = dsh.cost.get_stats()
print(stats)
# {
#   'today': {'date': '2024-01-01', 'calls': 5, 'quota': 1000, 'remaining': 995},
#   'total': {'calls': 23, 'tokens': 45678},
#   'by_checkpoint': {'cp1_preprocess': 12, 'cp4_quality': 11}
# }
```

## 🛠️ 下一步开发

### Phase 2: CP1 预处理（通过 Skill 实现）

创建 `.claude/skills/dsh-preprocess/`:
- `checkpoint.py`：标题层级修复逻辑
- `prompts/heading_fix.txt`：Prompt 模板
- `SKILL.md`：Skill 元数据

### Phase 3: CP4 质检（通过 Skill 实现）

创建 `.claude/skills/dsh-quality/`:
- `checkpoint.py`：PDF 质量评分逻辑
- `prompts/quality_check.txt`：Prompt 模板
- `SKILL.md`：Skill 元数据

### 扩展示例

- `dsh-reference-check`：参考文献格式检查
- `dsh-figure-validator`：图表标题完整性验证
- `dsh-content-compare`：原文与排版结果对比

## 🔒 安全性

- API Key 通过环境变量传递，不写入配置文件
- 结果缓存基于 prompt hash，不存储敏感内容
- 配额管理防止恶意调用

## 📝 License

MIT
