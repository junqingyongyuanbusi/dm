"""OAuth 一键授权接入:每平台一个模块(Postiz provider 同款拆分)。

- x.py:X OAuth 1.0a 三步流
- meta.py:Facebook Page / Instagram 专业账号(Facebook Login,OAuth 2.0)
- common.py:加密 state cookie、提示页等共享设施

Telegram 无 OAuth(BotFather token 模型),继续走手工表单 + provisioning。
"""

from fastapi import APIRouter

from social_reply.application.account_management.oauth import meta, x

router = APIRouter()
router.include_router(x.router)
router.include_router(meta.router)
