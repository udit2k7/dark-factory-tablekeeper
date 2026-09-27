from __future__ import annotations

import asyncio


async def hanging_app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        await asyncio.sleep(60)
