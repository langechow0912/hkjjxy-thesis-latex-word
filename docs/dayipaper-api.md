# 大以论文 API 接入

产品统一入口与运行方式见 [README](../README.md)。海口经济学院走学校排版系统；其他院校使用这里的服务方适配。

## 配置与鉴权

默认地址 `https://transport.b1n1.dayipaper.com`，可由 `DAYIPAPER_BASE` 覆盖。接口前缀 `/formatter/pat/open/document/format`。后端发送 `Authorization: Bearer <PAT>`，不把凭证传给前端、拼入 URL 或写入日志。

启动时 `core/localconfig.py` 加载忽略目录 `.secrets/dayipaper.pat`，支持原始值和 `PAT=值`。环境变量 `DAYIPAPER_PAT` 优先。注意 `.env` 不会自动加载。

## 已实测调用流程

| 步骤 | 服务方路径 | 返回 |
|---|---|---|
| 查询规范 | GET /searchRule | 分页 items，id 为规范 ID |
| 上传 | POST /uploadRowDocument | fileUri 字符串 |
| 发起排版 | POST /requestDocumentFormat | taskId 字符串 |
| 状态 | GET /traceTask | status、statusLabel、pdfShareUrl 等 |
| 建议 | GET /formatSuggestions | 列表 |
| 导出 | GET /downloadFormattedDocument | Word 下载地址；收费，真实导出待确认 |

响应信封 `{code,message,data}`，成功 code=0。排版请求必填 title、authorName、fileUri、ruleId。轮询间隔至少 1 秒，默认 2 秒；默认超时 900 秒。具体配置见 web/app.py。

预览每次重新取状态并跳转。导出下载地址现取现下，不对存储地址附带 PAT。成功导出缓存在任务目录，并发导出使用锁。页面显式确认收费后才调用导出。计费规则和链接有效期仍以服务方当前约定为准。

## 验证与限制

2026-10-21 使用用户已提供的真实 PAT，成功搜索海南大学 3 条规范，并对测试 Word 完成上传、发起、轮询到 COMPLETED、获取建议、下载 8 页 PDF。用户已选择暂不收费、保留预览结果，未调用真实收费导出。离线测试覆盖包括导出在内的客户端流程，但不能替代真实收费接口验收。

此前 401 源于将文件中的 `PAT=` 标签误当令牌值，修复后鉴权成功。不能仅凭 401 判定凭证失效；应核对加载值的格式、请求头、地址和权限，避免泄露令牌。

测试样稿封面部分字段未自动填充，正式交稿需核对。复杂表格、公式、图像及院校规则准确性还需真实代表性论文验收。

## 后续 API

统一规范 ID 通过 `school:` / `dayipaper:` 防止冲突。新增院校服务应适配规范查询、提交、状态、预览、下载和计费标记；其他功能 API 可复用配置和错误处理约定，但应建立相应业务接口。当前没有宣称已实现任意 API 的自动接入。
