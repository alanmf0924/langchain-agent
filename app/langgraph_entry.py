"""LangGraph Server 的调试入口，只暴露推荐工作流。"""

from app.service import SkinAssistantService

# LangGraph Server 通过 langgraph.json 导入这个编译后的图。
# 购物车草稿仍由 FastAPI 的确认接口负责，避免绕过用户确认。
service = SkinAssistantService()
graph = service.recommendation_graph
