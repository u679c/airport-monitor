# 机场节点联通性测试面板

支持：
- 订阅链接与检测参数持久化到 SQLite
- 自动定时 TCP 联通性检测
- 检测结果写入 SQLite（本地文件 `monitor.db`）
- 24 小时历史可视化（按页面宽度动态 5/10 分钟粒度）
- 节点名称筛选（浏览器本地记忆）

## 环境
- `python3.11`
- `pip3.11`（或 `python3.11 -m pip`）

## 安装依赖
```bash
python3.11 -m pip install -r requirements.txt
```

## 启动
```bash
python3.11 app.py
```
打开：`http://127.0.0.1:3000`

## 配置持久化
- 配置项（`subscription_url`、`interval_sec`、`timeout_ms`、`concurrency`、`max_nodes`）已写入数据库表 `app_settings`。
- 旧版本若存在 `monitor_config.json`，启动时会自动迁移到数据库。

## 使用
1. 页面可读取并编辑检测配置。
2. 自动按 `interval_sec` 周期检测（后台独立调度，不依赖前端操作）。
3. 点击“立即检测”可手动触发；若当前有筛选，可仅检测筛选节点。

## 数据文件与数据表
启动后会自动创建：
- `monitor.db`（SQLite 数据库文件）

库内表：
- `checks`：每轮检测汇总
- `node_results`：每个节点在每轮的检测结果
- `app_settings`：系统配置持久化

## 开源前注意
仓库已通过 `.gitignore` 排除了本地数据与敏感文件（如 `monitor.db`、`monitor_config.json`、`.env`）。
