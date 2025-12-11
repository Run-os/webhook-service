# FastAPI Webhook Service

基于 FastAPI 的 WebSocket 推送服务，使用 Redis 存储 token。

## 功能

- WebSocket 连接：`/stream?token=<clientToken>`
- POST 推送：`/message?token=<appToken>`
- 自动创建 token 对
- 每周自动清理数据

## 部署

部署到 Zeabur，需要连接 Redis 服务。

## API

### WebSocket 连接

ws://your-domain/stream?token=your-client-token

### 发送消息

```bash
curl -X POST "http://your-domain/message?token=your-app-token" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "测试标题",
    "message": "测试消息",
    "priority": 2
  }'
```
