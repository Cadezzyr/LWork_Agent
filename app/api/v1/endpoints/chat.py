"""对话接口：同步回复 + SSE 流式回复（都需要登录）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import AgentServiceDep, CurrentUserDep
from app.schemas.chat import ChatRequest, ChatResponse, ResumeRequest, StreamEventData
from app.utils.sse import format_sse

router = APIRouter(tags=["对话"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="同步对话（返回完整结果）",
    responses={
        401: {"description": "未登录或登录态失效"},
        404: {"description": "会话不存在或不属于当前用户"},
        502: {"description": "模型调用失败"},
        503: {"description": "模型或智能体未就绪"},
    },
)
async def chat(
    payload: ChatRequest,
    current: CurrentUserDep,
    service: AgentServiceDep,
) -> ChatResponse:
    """一次性返回本轮完整结果：回复文本、工具调用轨迹、token 用量。

    多轮对话请复用响应里的 ``session_id``；该会话必须属于当前登录用户。
    """
    return await service.chat(
        user_id=current.user.id,
        message=payload.message,
        session_id=payload.session_id,
    )


@router.post(
    "/chat/stream",
    summary="流式对话（Server-Sent Events）",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "SSE 流；事件类型：session / token / tool_call / tool_result / final / error / done"
            ),
            "content": {"text/event-stream": {"schema": StreamEventData.model_json_schema()}},
        }
    },
)
async def chat_stream(
    payload: ChatRequest,
    current: CurrentUserDep,
    service: AgentServiceDep,
) -> StreamingResponse:
    """以 SSE 形式流式返回 token、工具调用与最终结果。

    浏览器 ``EventSource`` 只支持 GET，POST 场景请用 ``fetch`` + ``ReadableStream`` 读取。

    注意两点（前端需要按 ``Content-Type`` 区分处理）：
    1. 认证与**会话归属校验都在开流之前**完成，失败返回普通 JSON（401/404），不是 SSE；
    2. 流中的 ``error`` 事件用于模型侧异常或落库失败（此时 HTTP 状态码已是 200）。
    """
    # 先解析/创建会话：越权与非法 session_id 在这里就以 404 抛出，而不是变成半截流
    conversation = await service.prepare_conversation(
        current.user.id,
        session_id=payload.session_id,
        message=payload.message,
    )
    stream = service.chat_stream(
        user_id=current.user.id,
        message=payload.message,
        session_id=conversation.id,
        conversation=conversation,
    )
    return StreamingResponse(_as_sse(stream), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "/chat/resume",
    summary="人工确认后继续对话（Server-Sent Events）",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "SSE 流；事件类型：token / tool_call / tool_result / interrupt / final / error / done"
            ),
            "content": {"text/event-stream": {"schema": StreamEventData.model_json_schema()}},
        },
        400: {"description": "决策数量或类型与该工具的 allowed_decisions 不符"},
        404: {"description": "会话不存在或不属于当前用户"},
        409: {"description": "该会话当前没有待确认的工具调用"},
    },
)
async def chat_resume(
    payload: ResumeRequest,
    current: CurrentUserDep,
    service: AgentServiceDep,
) -> StreamingResponse:
    """提交 approve / edit / reject 决策，继续被 HumanInTheLoopMiddleware 暂停的那一轮。

    决策顺序必须与上一条 ``interrupt`` 事件里 ``action_requests`` 的顺序一致；
    恢复后如果模型又调用了需要确认的工具，流里会再次出现 ``interrupt`` 事件。
    """
    # 会话归属、是否仍有待确认调用、决策是否合法都在开流之前校验，错误直接是 JSON
    conversation, resume = await service.prepare_resume(
        current.user.id,
        session_id=payload.session_id,
        decisions=payload.decisions,
    )
    stream = service.chat_resume_stream(
        user_id=current.user.id,
        conversation=conversation,
        resume=resume,
    )
    return StreamingResponse(_as_sse(stream), media_type="text/event-stream", headers=_SSE_HEADERS)


async def _as_sse(stream: AsyncIterator[tuple[str, dict[str, object]]]) -> AsyncIterator[str]:
    """在异常/断开时确保内层生成器被关闭，从而释放会话锁。"""
    try:
        async for event, data in stream:
            yield format_sse(event, data)
    finally:
        await stream.aclose()
