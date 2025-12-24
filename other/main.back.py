# 导入所需的库和模块
# FastAPI 用于构建API，WebSocket用于实时通信，HTTPException处理HTTP错误
# Query用于处理查询参数，Request用于请求对象
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
# JSONResponse用于返回JSON格式响应
from fastapi.responses import JSONResponse
# BaseModel用于数据验证和模型定义
from pydantic import BaseModel
# 异步Redis客户端，用于数据存储
import redis.asyncio as redis
# base64用于令牌的编码解码
import base64
# json用于JSON数据处理
import json
# asyncio用于异步任务处理
import asyncio
# datetime用于处理时间戳
from datetime import datetime
# typing模块提供类型提示功能
from typing import Dict, Set
# logging用于日志记录
import logging
# os用于处理环境变量
import os
# re用于正则表达式匹配（CORS域名验证）
import re
# 用于自定义HTTP中间件
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# 配置日志系统，设置日志级别为INFO（记录一般信息）
logging.basicConfig(level=logging.INFO)
# 创建日志记录器实例，用于记录应用程序日志
logger = logging.getLogger(__name__)

# 创建FastAPI应用实例，设置API标题
app = FastAPI(title="Webhook Service")

# 自定义CORS中间件，支持通配符子域名
# 作用：处理跨域资源共享，允许特定域名的前端应用访问后端API
# 注意事项：
# 1. 严格控制允许的域名，避免安全风险
# 2. 正确处理预检请求(OPTIONS方法)，否则浏览器会阻止跨域请求


class CustomCORSMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        # 允许的域名模式（正则表达式）
        self.allowed_origin_patterns = [
            re.compile(r"^https://.*\.zeabur\.app$"),  # 允许所有zeabur.app的子域名
            re.compile(r"^https://.*\.730406\.xyz$"),  # 允许所有730406.xyz的子域名
            re.compile(r"^http://localhost(:\d+)?$"),  # 允许本地开发环境
            re.compile(r"^http://127\.0\.0\.1(:\d+)?$"),  # 允许本地开发环境
        ]

    # 检查请求来源是否在允许的域名列表中
    def is_origin_allowed(self, origin: str) -> bool:
        if not origin:
            return True
        for pattern in self.allowed_origin_patterns:
            if pattern.match(origin):
                return True
        return False

    # 处理所有请求，添加CORS响应头
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # 处理预检请求 (OPTIONS)
        if request.method == "OPTIONS":
            response = Response(status_code=200)
            if self.is_origin_allowed(origin):
                response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
            else:
                response.headers["Access-Control-Allow-Origin"] = "*"
            # 允许的HTTP方法
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            # 允许的请求头
            response.headers["Access-Control-Allow-Headers"] = "*"
            # 允许携带凭证（如cookies）
            response.headers["Access-Control-Allow-Credentials"] = "true"
            # 预检请求结果的缓存时间（10分钟）
            response.headers["Access-Control-Max-Age"] = "600"
            return response

        # 处理正常请求
        response = await call_next(request)

        # 添加CORS头信息
        if self.is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        # 允许前端访问的响应头
        response.headers["Access-Control-Expose-Headers"] = "*"
        return response


# 将自定义CORS中间件添加到应用中
app.add_middleware(CustomCORSMiddleware)

# Redis连接客户端（全局变量，启动时初始化）
redis_client = None

# WebSocket连接管理器
# 作用：管理所有WebSocket连接，包括连接建立、断开和消息发送
# 注意事项：
# 1. 需要线程安全的结构存储连接，但由于FastAPI单线程异步特性，使用普通字典即可
# 2. 及时清理断开的连接，避免内存泄漏


class ConnectionManager:
    def __init__(self):
        # 存储活跃连接：键为client_token，值为该client的所有WebSocket连接集合
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    # 建立新连接
    async def connect(self, client_token: str, websocket: WebSocket):
        # 接受WebSocket连接
        await websocket.accept()
        # 如果该client_token没有连接记录，创建一个新集合
        if client_token not in self.active_connections:
            self.active_connections[client_token] = set()
        # 将新连接添加到集合中
        self.active_connections[client_token].add(websocket)
        # 记录日志
        logger.info(
            f"Client {client_token} connected. Total connections: {len(self.active_connections[client_token])}")

    # 断开连接
    def disconnect(self, client_token: str, websocket: WebSocket):
        if client_token in self.active_connections:
            # 从集合中移除连接
            self.active_connections[client_token].discard(websocket)
            # 如果该client_token没有活跃连接了，删除该记录
            if not self.active_connections[client_token]:
                del self.active_connections[client_token]
        logger.info(f"Client {client_token} disconnected")

    # 向特定client_token的所有连接发送消息
    async def send_message(self, client_token: str, message: dict):
        if client_token in self.active_connections:
            # 记录断开的连接，后续清理
            disconnected = set()
            for connection in self.active_connections[client_token]:
                try:
                    # 发送JSON格式消息
                    await connection.send_json(message)
                except Exception as e:
                    # 发送失败，记录错误并标记为断开
                    logger.error(f"Error sending message: {e}")
                    disconnected.add(connection)

            # 清理所有断开的连接
            for conn in disconnected:
                self.disconnect(client_token, conn)


# 创建连接管理器实例
manager = ConnectionManager()

# 请求体模型（用于验证POST /message接口的请求数据）
# 作用：自动验证请求数据格式和类型，不符合时返回422错误
# 注意事项：
# 1. 定义的字段类型和默认值会影响接口文档和数据验证
# 2. 所有客户端发送的请求都必须符合该模型定义


class Message(BaseModel):
    message: str  # 消息内容（必填）
    priority: int = 2  # 优先级（可选，默认值2）
    title: str = "通知"  # 标题（可选，默认值"通知"）


# 应用启动时执行的事件
@app.on_event("startup")
async def startup_event():
    global redis_client
    # 连接Redis - 从环境变量获取REDIS_URI，没有则使用默认值
    redis_url = os.getenv("REDIS_URI", "redis://localhost:6379")
    try:
        # 创建Redis异步客户端
        redis_client = await redis.from_url(redis_url, decode_responses=True)
        # 测试连接是否成功
        await redis_client.ping()
        logger.info(f"Redis connected successfully to {redis_url}")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        # 连接失败时终止应用启动
        raise

    # 启动定时清理任务（每周清空一次Redis数据）
    asyncio.create_task(weekly_cleanup())


# 应用关闭时执行的事件
@app.on_event("shutdown")
async def shutdown_event():
    # 关闭Redis连接
    if redis_client:
        await redis_client.close()


# 每周清理任务
async def weekly_cleanup():
    """每周清空一次所有Redis数据"""
    while True:
        try:
            # 等待7天（单位：秒）
            await asyncio.sleep(7 * 24 * 60 * 60)
            if redis_client:
                # 清空整个数据库
                await redis_client.flushdb()
                logger.info("Weekly cleanup completed")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


# 创建令牌对（clientToken和appToken）
async def create_token_pair(client_token: str) -> str:
    """创建 clientToken 和对应的 appToken"""
    # appToken是clientToken的base64编码
    app_token = base64.b64encode(client_token.encode()).decode()

    # 存储到Redis
    if redis_client:
        # 以"client:{client_token}"为键存储客户端信息
        await redis_client.set(f"client:{client_token}", json.dumps({
            "app_token": app_token,
            "created_at": datetime.now().isoformat()  # 创建时间
        }))
        # 以"app:{app_token}"为键存储反向映射（用于通过appToken查找clientToken）
        await redis_client.set(f"app:{app_token}", client_token)
        logger.info(
            f"Created token pair - client: {client_token}, app: {app_token}")

    return app_token


# 通过appToken获取clientToken
async def get_client_token(app_token: str) -> str:
    """通过 appToken 获取 clientToken"""
    if redis_client:
        # 从Redis中查找
        client_token = await redis_client.get(f"app:{app_token}")
        if client_token:
            return client_token

    # 如果Redis中没有，尝试从appToken反向解码得到clientToken
    try:
        client_token = base64.b64decode(app_token.encode()).decode()
        # 自动创建token对（如果解码成功）
        if redis_client:
            await redis_client.set(f"client:{client_token}", json.dumps({
                "app_token": app_token,
                "created_at": datetime.now().isoformat()
            }))
            await redis_client.set(f"app:{app_token}", client_token)
            logger.info(
                f"Auto-created token pair from appToken - client: {client_token}, app: {app_token}")
        return client_token
    except Exception as e:
        logger.error(f"Failed to decode appToken: {e}")
        return None


# 检查clientToken是否存在
async def token_exists(client_token: str) -> bool:
    """检查 clientToken 是否存在"""
    if redis_client:
        # 检查Redis中是否存在该键
        exists = await redis_client.exists(f"client:{client_token}")
        return bool(exists)
    return False


# 根路由（首页）
@app.get("/")
async def root():
    return {
        "service": "FastAPI Webhook Service",
        "version": "1.0.0",
        "endpoints": {
            "websocket": "/stream?token=<clientToken>",  # WebSocket连接端点
            "post": "/message?token=<appToken>"  # 发送消息的POST端点
        },
        "status": "running"
    }


# WebSocket连接端点
@app.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """WebSocket 连接端点：客户端通过此端点建立长连接以接收消息"""
    # 从查询参数获取client_token
    client_token = token

    # 检查token是否存在，不存在则创建
    if not await token_exists(client_token):
        app_token = await create_token_pair(client_token)
        logger.info(
            f"New client token created: {client_token}, app token: {app_token}")

    # 管理连接
    await manager.connect(client_token, websocket)

    try:
        # 发送连接成功的欢迎消息
        await websocket.send_json({
            "type": "connected",
            "message": "WebSocket connected successfully",
            "client_token": client_token,
            "timestamp": datetime.now().isoformat()
        })

        # 保持连接：循环接收客户端消息（可用于心跳检测）
        while True:
            data = await websocket.receive_text()
            # 记录客户端发送的消息
            logger.info(f"Received from {client_token}: {data}")

    except WebSocketDisconnect:
        # 客户端主动断开连接
        manager.disconnect(client_token, websocket)
        logger.info(f"Client {client_token} disconnected")
    except Exception as e:
        # 其他错误导致连接断开
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(client_token, websocket)


# 发送消息的POST接口
@app.post("/message")
async def send_message(message: Message, token: str = Query(...)):
    """接收POST请求并推送到对应的WebSocket客户端"""
    app_token = token

    # 通过appToken获取client_token
    client_token = await get_client_token(app_token)

    if not client_token:
        # appToken无效
        raise HTTPException(status_code=400, detail="Invalid app token format")

    # 构造要发送的消息数据
    msg_data = {
        "type": "message",
        "title": message.title,
        "message": message.message,
        "priority": message.priority,
        "timestamp": datetime.now().isoformat()  # 添加时间戳
    }

    # 检查是否有活跃的连接
    if client_token not in manager.active_connections or not manager.active_connections[client_token]:
        logger.warning(
            f"No active WebSocket connection for client {client_token}, message not delivered")
        return JSONResponse(
            status_code=200,
            content={
                "status": "no_connection",
                "message": "Message received but no active WebSocket connection to deliver",
                "client_token": client_token,
                "connections": 0
            }
        )

    # 发送到对应的WebSocket连接
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


# 健康检查接口
@app.get("/health")
async def health_check():
    """健康检查接口：用于监控服务状态"""
    redis_status = "connected"
    try:
        if redis_client:
            # 测试Redis连接
            await redis_client.ping()
    except:
        redis_status = "disconnected"

    return {
        "status": "healthy",  # 服务状态
        "redis": redis_status,  # Redis连接状态
        "active_clients": len(manager.active_connections),  # 活跃客户端数量
        # 总连接数
        "total_connections": sum(len(conns) for conns in manager.active_connections.values())
    }


# 获取token信息的接口（调试用）
@app.get("/tokens/{client_token}")
async def get_token_info(client_token: str):
    """获取 token 信息（调试用）"""
    if not await token_exists(client_token):
        raise HTTPException(status_code=404, detail="Token not found")

    if redis_client:
        # 从Redis获取token信息
        data = await redis_client.get(f"client:{client_token}")
        token_data = json.loads(data)
        return {
            "client_token": client_token,
            "app_token": token_data["app_token"],
            "created_at": token_data["created_at"],
            "has_connection": client_token in manager.active_connections  # 是否有活跃连接
        }
