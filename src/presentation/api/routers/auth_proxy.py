import httpx
from fastapi import APIRouter, Request, Response

router = APIRouter()
INTERNAL_AUTH_URL = "http://auth:9999"

@router.api_route("/auth/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_gotrue(request: Request, path: str):
    async with httpx.AsyncClient(base_url=INTERNAL_AUTH_URL) as client:
        url = f"/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        content = await request.body()
        
        try:
            response = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                params=request.query_params,
                content=content,
                timeout=30.0
            )
        except Exception as e:
            return Response(content=f"Auth Proxy Error: {str(e)}", status_code=502)
        
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        response_headers = {k: v for k, v in response.headers.items() if k.lower() not in excluded_headers}
        return Response(content=response.content, status_code=response.status_code, headers=response_headers)
