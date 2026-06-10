# TickTick API 迁移计划

## 项目状态

- **当前**: Google Calendar API 集成
- **目标**: 替换为 TickTick API（保留 Google Service 代码但不作为默认同步方式）
- **分支**: `ticktick-migration`

## 文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `index.html` | 修改 | 新增 TickTick 连接按钮 + 状态显示 |
| `server.py` | 修改 | 新增 TickTick 路由 + 任务接口改造 |
| `api/index.py` | 修改 | Vercel 版本同步更新 |
| `ticktick_service.py` | 新增 | TickTick API 封装 |
| `requirements.txt` | 修改 | 新增加密库依赖 |

## Phase 1: TickTick OAuth

- [ ] `ticktick_service.py` — 封装 TickTick API（OAuth token 管理、CRUD）
- [ ] `server.py` — 新增 `/api/ticktick/auth`, `/api/ticktick/callback`, `/api/ticktick/status`
- [ ] `api/index.py` — 同上（Vercel 版）
- [ ] `.env` — 新增加密密钥 + TickTick OAuth 变量

## Phase 2: 数据库迁移

- [ ] tasks 表新增字段：`ticktick_task_id`, `ticktick_project_id`, `sync_provider`, `sync_status`, `last_synced_at`, `sync_error`
- [ ] ticktick_tokens 表（新）：存储加密的 access_token / refresh_token

## Phase 3: 任务创建同步

- [ ] `POST /api/tasks` — 新增路由，保存本地 + 同步 TickTick
- [ ] `ticktickService.createTask()` — 实现

## Phase 4-6: 更新/完成/删除同步

- [ ] `PATCH /api/tasks/:id` — 更新本地 + 同步 TickTick
- [ ] `PATCH /api/tasks/:id/complete` — 完成本地 + 同步 TickTick
- [ ] `DELETE /api/tasks/:id` — 删除本地 + 同步 TickTick

## Phase 7: 重试机制

- [ ] `POST /api/tasks/:id/resync` — 单任务重试
- [ ] `POST /api/tasks/resync-failed` — 批量重试

## 前端

- [ ] 新增 "连接 TickTick" 按钮
- [ ] 连接状态显示
- [ ] 按钮文案改为 "📅 写入日历"
- [ ] Toast 提示改为 TickTick
