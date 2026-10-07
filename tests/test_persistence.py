from datetime import timedelta

import pytest

from app.persistence import SqlAssistantRepository, metadata
from app.service import SkinAssistantService


def test_run_audit_usage_and_confirmation_survive_service_recreation(tmp_path) -> None:
    """重建 Service 模拟进程重启：公开轨迹和未消费确认令牌不能随之丢失。"""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'assistant.sqlite3'}"
    repository = SqlAssistantRepository(url=database_url)
    metadata.create_all(repository.engine)
    first_service = SkinAssistantService(
        enable_cart_drafts=True,
        repository=repository,
    )
    first_service.answer_chain._chain = None
    run_id = "run_persisted_recommendation"
    result = first_service.build_result("换季干燥紧绷，预算 600 元", run_id)
    token = result.actions[0]["confirmation_token"]

    restarted_service = SkinAssistantService(
        enable_cart_drafts=True,
        repository=SqlAssistantRepository(url=database_url),
    )
    trace = restarted_service.get_trace(run_id)
    assert trace is not None
    assert trace.finished_at is not None
    assert any(item.run_id == run_id for item in restarted_service.audit_logs)
    assert any(item.run_id == run_id for item in restarted_service.model_usage_logs)

    cart_draft_id, _ = restarted_service.create_cart_draft(token, confirmed=True)
    assert cart_draft_id.startswith("cart_draft_")
    with pytest.raises(KeyError, match="已使用"):
        restarted_service.create_cart_draft(token, confirmed=True)


def test_expired_confirmation_is_recorded_as_non_executable(tmp_path) -> None:
    """过期令牌必须无法写草稿，且不能靠前端重放 token 恢复执行权限。"""
    repository = SqlAssistantRepository(url=f"sqlite+pysqlite:///{tmp_path / 'assistant.sqlite3'}")
    metadata.create_all(repository.engine)
    service = SkinAssistantService(enable_cart_drafts=True, repository=repository)
    service.answer_chain._chain = None
    bundle = service.build_result("换季干燥紧绷，预算 600 元", "run_expired_source").bundles[0]
    now = service.now()
    repository.create_confirmation(
        token="confirm_expired_token",
        bundle=bundle,
        run_id="run_expired",
        created_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=5),
    )

    with pytest.raises(KeyError, match="过期"):
        service.create_cart_draft("confirm_expired_token", confirmed=True)
