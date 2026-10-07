"""OA API 的统一响应契约。

Pure Admin 的接口约定以 ``code``、``message``、``data`` 为核心。本模块在此基础上
补充 HTTP ``status`` 与 ``requestId``，让业务用户看到可读提示，也让前端能追踪一次
失败请求；实际 HTTP 状态码仍保持语义正确，不能只依赖 JSON 字段判断错误。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute

SUCCESS_CODE = 0


def request_id_for(request: Request) -> str:
    """返回由中间件生成的请求标识，供路由和异常处理共用。"""
    return getattr(request.state, "request_id", "")


def response_body(
    *,
    status_code: int,
    message: str,
    data: Any,
    request_id: str,
    code: int = SUCCESS_CODE,
    errors: Any | None = None,
) -> dict[str, Any]:
    """构建稳定的 OA HTTP JSON 信封，避免不同接口各自拼装字段。"""
    body: dict[str, Any] = {
        "code": code,
        "status": status_code,
        "message": message,
        "data": data,
        "requestId": request_id,
    }
    if errors is not None:
        body["errors"] = errors
    return body


class OaApiRoute(APIRoute):
    """将 `/api/v1` 成功 JSON 响应包装为 Pure Admin 可消费的统一信封。

    端点仍使用各自的 Pydantic ``response_model`` 校验原始业务数据；在最后一层统一包装，
    既不重复每个端点的样板代码，也不改变状态码、Cookie 或审计行为。
    """

    def get_route_handler(self):  # type: ignore[no-untyped-def]
        original_handler = super().get_route_handler()

        async def custom_route_handler(request: Request):  # type: ignore[no-untyped-def]
            response = await original_handler(request)
            content_type = response.headers.get("content-type", "")
            # 204 不允许响应体；OA 的写操作需要向调用者回传 message，故规范化成 200。
            response_status = 200 if response.status_code == 204 else response.status_code
            if response.status_code == 204:
                data = None
            else:
                if "application/json" not in content_type:
                    return response
                try:
                    data = json.loads(response.body)
                except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                    return response

            response.body = json.dumps(
                response_body(
                    status_code=response_status,
                    message="操作成功",
                    data=data,
                    request_id=request_id_for(request),
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            response.status_code = response_status
            response.headers["content-type"] = "application/json; charset=utf-8"
            response.headers["content-length"] = str(len(response.body))
            return response

        return custom_route_handler
