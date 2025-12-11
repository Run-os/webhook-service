from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import redis.asyncio as redis
import base64
import json
import asyncio
from datetime import datetime
from typing import Dict, Set
import logging
import os

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Webhook Service")

# Redis 连接
redis_client = None

# WebSocket 连接管理
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, client_token: str, websocket: WebSocket):
        await websocket.accept()
        if client_token not in self.active_connections:
            self.active_connections[client_token] = set()
        self.active_connections[client_token].add(websocket)
        logger.info(f"Client {client_token} connected. Total connections: {len(self.active_connections[client_token])}")
    
    def disconnect(self, client_token: str, websocket: WebSocket):
        if client_token in self.active_connections:
            self.active_connections[client_token].discard(websocket)
            if not self.active_connections[client_token]:
                del self.active_connections[client_token]
        logger.info(f"Client {client_token} disconnected")
    
    async def send_message(self, client_token: str, message: dict):
        if client_token in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[client_token]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"Error sending message: {e}")
                    disconnected.add(connection)
            
            # 清理断开的连接
            for conn in disconnected:
                self.disconnect(client_token, conn)

manager = ConnectionManager()

# 请求体模型
class Message(BaseModel):
    message: str
    priority: int = 2
    title: str = "通知"

@app.on_event("startup")
async def startup_event():
    global redis_client
    # 连接 Redis - Zeabur 会自动注入 REDIS_URI
    redis_url = os.getenv("REDIS_URI", "redis://localhost:6379")
    try:
        redis_client = await redis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info(f"Redis connected successfully to {redis_url}")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        raise
    
    # 启动定时清理任务
    asyncio.create_task(weekly_cleanup())

@app.on_event("shutdown")
async def shutdown_event():
    if redis_client:
        await redis_client.close()

async def weekly_cleanup():
    """每周清空一次所有数据"""
    while True:
        try:
            # 等待 7 天
            await asyncio.sleep(7 * 24 * 60 * 60)
            if redis_client:
                await redis_client.flushdb()
                logger.info("Weekly cleanup completed")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

async def create_token_pair(client_token: str) -> str:
    """创建 clientToken 和对应的 appToken"""
    # appToken 为 clientToken 的 base64 编码
    app_token = base64.b64encode(client_token.encode()).decode()
    
    # 存储到 Redis
    if redis_client:
        await redis_client.set(f"client:{client_token}", json.dumps({
            "app_token": app_token,
            "created_at": datetime.now().isoformat()
        }))
        await redis_client.set(f"app:{app_token}", client_token)
        logger.info(f"Created token pair - client: {client_token}, app: {app_token}")
    
    return app_token

async def get_client_token(app_token: str) -> str:
    """通过 appToken 获取 clientToken"""
    if redis_client:
        client_token = await redis_client.get(f"app:{app_token}")
        return client_token
    return None

async def token_exists(client_token: str) -> bool:
    """检查 clientToken 是否存在"""
    if redis_client:
        exists = await redis_client.exists(f"client:{client_token}")
        return bool(exists)
    return False

@app.get("/")
async def root():
    return {
        "service": "FastAPI Webhook Service",
        "version": "1.0.0",
        "endpoints": {
            "websocket": "/stream?token=<clientToken>",
            "post": "/message?token=<appToken>"
        },
        "status": "running"
    }

@app.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """WebSocket 连接端点"""
    client_token = token
    
    # 检查 token 是否存在，不存在则创建
    if not await token_exists(client_token):
        app_token = await create_token_pair(client_token)
        logger.info(f"New client token created: {client_token}, app token: {app_token}")
    
    await manager.connect(client_token, websocket)
    
    try:
        # 发送欢迎消息
        await websocket.send_json({
            "type": "connected",
            "message": "WebSocket connected successfully",
            "client_token": client_token,
            "timestamp": datetime.now().isoformat()
        })
        
        # 保持连接
        while True:
            data = await websocket.receive_text()
            # 可以处理客户端发送的消息（心跳等）
            logger.info(f"Received from {client_token}: {data}")
            
    except WebSocketDisconnect:
        manager.disconnect(client_token, websocket)
        logger.info(f"Client {client_token} disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(client_token, websocket)

@app.post("/message")
async def send_message(message: Message, token: str = Query(...)):
    """接收 POST 请求并推送到对应的 WebSocket 客户端"""
    app_token = token
    
    # 通过 appToken 获取 clientToken
    client_token = await get_client_token(app_token)
    
    if not client_token:
        raise HTTPException(status_code=404, detail="Invalid app token")
    
    # 检查是否有活跃的连接
    if client_token not in manager.active_connections or not manager.active_connections[client_token]:
        raise HTTPException(status_code=404, detail="No active WebSocket connection for this token")
    
    # 构造消息
    msg_data = {
        "type": "message",
        "title": message.title,
        "message": message.message,
        "priority": message.priority,
        "timestamp": datetime.now().isoformat()
    }
    
    # 发送到对应的 WebSocket 连接
    await manager.send_message(client_token, msg_data)
    
    logger.info(f"Message sent to client {client_token}: {message.title}")
    
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "message": "Message sent successfully",
            "client_token": client_token,
            "connections": len(manager.active_connections.get(client_token, []))
        }
    )

@app.get("/health")
async def health_check():
    """健康检查"""
    redis_status = "connected"
    try:
        if redis_client:
            await redis_client.ping()
    except:
        redis_status = "disconnected"
    
    return {
        "status": "healthy",
        "redis": redis_status,
        "active_clients": len(manager.active_connections),
        "total_connections": sum(len(conns) for conns in manager.active_connections.values())
    }

@app.get("/tokens/{client_token}")
async def get_token_info(client_token: str):
    """获取 token 信息（调试用）"""
    if not await token_exists(client_token):
        raise HTTPException(status_code=404, detail="Token not found")
    
    if redis_client:
        data = await redis_client.get(f"client:{client_token}")
        token_data = json.loads(data)
        return {
            "client_token": client_token,
            "app_token": token_data["app_token"],
            "created_at": token_data["created_at"],
            "has_connection": client_token in manager.active_connections
        }
