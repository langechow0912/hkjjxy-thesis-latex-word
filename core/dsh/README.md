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
  __init__.py           # DSHManager（扫描 skills/ 注册检查点、调度）
  client.py             # DeepSeek API 封装
  cost_control.py       # 配额管理（全局日配额 / 单任务 / 每用户每日）与成本统计
  outline.py            # 把原稿压成「结构清单」（模型只看这个，不看正文）
  runner.py             # 同步排版流程里调用 DSH；save_outline/load_outline 落盘结构
  checkpoints/
    __init__.py         # Checkpoint 基类
  skills/               # 每个 DSH 功能一个目录，新增功能 = 新增目录
    __init__.py         # discover()/load_module()
    dsh-preprocess/     # CP1 标题层级 / 编号检查
      SKILL.md          #   frontmatter：name/description/version/checkpoint
      checkpoint.py     #   Checkpoint 子类
      prompts/heading_fix.txt
    dsh-quality/        # CP4 质检（占位，默认不启用）
web/dsh_routes.py       # /api/dsh/*：skills、jobs、outline、run、chat
web/static/dsh.html|js  # 调试页 /dsh：目录树、skill 列表、对话、调试信息
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
   - 在 `core/dsh/skills/dsh-xxx/` 下放 `SKILL.md` + `checkpoint.py`（+ `prompts/`）
   - 继承 `Checkpoint` 基类并实现 `name`, `should_run`, `execute`
   - DSHManager 自动扫描加载；单个 skill 加载失败只跳过它
   - 放在 `core/` 下是因为 Dockerfile 只 COPY `core/ web/ schools/`，`.claude/` 不进镜像

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

创建 `core/dsh/skills/dsh-my-check/`，里面放 `SKILL.md`（frontmatter 写 name/description/version/checkpoint）和 `checkpoint.py`：

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

## 🧪 调试页 `/dsh`

登录用户可打开 `/dsh`：选一个自己的本地排版任务，查看 DSH 识别出的目录树（标出流水线判定级别与 Word 原大纲级别的差异）、模型实际收到的结构清单、
已注册的 skill（可手动运行已启用的检查点），以及和 DSH 对话。

- 排版时 `schoolformat.run` 会把结构清单写进任务目录的 `dsh_outline.json`（只含标题文字和各段字数，不含正文）；只有此后新排版的任务才有。
- 对话与手动运行会真实调用 DeepSeek：受全局日配额和 `dsh.chat.per_user_daily`（默认 20 次/人/天）限制；模型只收到结构清单。
- 只能访问自己名下的任务，别人的任务一律 404。

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

已完成：`core/dsh/skills/dsh-preprocess/`（`checkpoint.py` / `prompts/heading_fix.txt` / `SKILL.md`）。

### Phase 3: CP4 质检（通过 Skill 实现）

目前 `core/dsh/skills/dsh-quality/` 只是占位：
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
