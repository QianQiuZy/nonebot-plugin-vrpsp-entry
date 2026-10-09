from pydantic import BaseModel, Field, field_validator


class Config(BaseModel):
    # 唯一权限来源：群主、管理员、NoneBot SUPERUSER 均没有额外权限。
    vrpsp_entry_allowed_users: set[int] = Field(default_factory=lambda: {308025580})
    vrpsp_entry_redis_url: str = "redis://127.0.0.1:6379/0"
    vrpsp_entry_key_prefix: str = "vrpsp_entry:"
    vrpsp_entry_poll_seconds: float = Field(default=30, ge=5, le=300)
    vrpsp_entry_http_timeout: float = Field(default=10, gt=0, le=20)
    vrpsp_entry_sources: set[str] = Field(default_factory=lambda: {"vr", "psp"})
    vrpsp_entry_vr_url: str = "https://vr.qianqiuzy.cn/gift/entry"
    vrpsp_entry_psp_url: str = "https://psp.qianqiuzy.cn/gift/entry"

    @field_validator("vrpsp_entry_key_prefix")
    @classmethod
    def nonempty_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Redis key prefix must not be empty")
        return value if value.endswith(":") else value + ":"

    @field_validator("vrpsp_entry_sources")
    @classmethod
    def valid_sources(cls, value: set[str]) -> set[str]:
        if not value <= {"vr", "psp"} or not value:
            raise ValueError("sources must contain vr and/or psp")
        return value


def is_allowed(config: Config, user_id: int) -> bool:
    return user_id in config.vrpsp_entry_allowed_users
